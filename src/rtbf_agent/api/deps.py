"""FastAPI dependency: yields a `Session` against the exact same DB `cli.py` reads/writes.

No second database, no independent config mechanism (issue #53): this module's only job is to
turn `rtbf_agent.db.build_engine()` (itself just `cli.py`'s original `_resolve_database_url()` +
`create_engine()`/`create_all()` logic, extracted so both callers share it -- see `rtbf_agent/
db.py`'s module docstring) into a request-scoped `Session` via FastAPI's `Depends(...)`.

The engine is built lazily and cached at module scope (`_engine`) rather than per-request --
this is a single self-hosted process (per this project's "no centralized backend" ethos), so one
long-lived engine/connection pool for the process lifetime is the right default, exactly like
`cli.py`'s `main_async()` builds one engine for its whole run. `_reset_engine_cache()` exists
purely so tests can force a fresh engine after changing `DATABASE_URL` mid-test-session; nothing
in the running app ever calls it.

Caveat: this assumes a file-backed (or real server) `DATABASE_URL`, matching this project's
documented default (`.env.example`'s `sqlite:///./data/rtbf-agent.db`). An in-memory
`sqlite://` URL would not be safe with this caching strategy under FastAPI's default
sync-route-in-a-threadpool execution -- SQLite's in-memory database isn't shared across
connections/threads at all, so a second thread checking out a new connection from the pool would
see an empty, table-less database. Not a concern for the documented/real deployment path, but
worth knowing if `DATABASE_URL` is ever pointed at `sqlite://` outside of tests (which instead use
`StaticPool` + `check_same_thread=False` explicitly -- see `tests/api/test_app.py`).
"""

from __future__ import annotations

import threading
from collections.abc import Generator

from fastapi import Depends
from sqlalchemy import Engine
from sqlmodel import Session

from rtbf_agent.core.interfaces import LLMProvider, WebSearchProvider
from rtbf_agent.db import build_engine
from rtbf_agent.plugins.jurisdiction_detection.detector import JurisdictionDetector
from rtbf_agent.plugins.llm.config import build_llm_provider, resolve_llm_config
from rtbf_agent.plugins.webfetch.http import HTTPWebFetchProvider
from rtbf_agent.plugins.websearch.config import resolve_searxng_config
from rtbf_agent.plugins.websearch.searxng import SearXNGProvider

_engine: Engine | None = None
# Guards `_engine`'s check-then-act initialization below. FastAPI runs sync `def` route handlers
# (like `get_session`'s callers) in a thread pool, so the first few concurrent requests could race
# to initialize `_engine` -- without a lock, two threads could both observe `_engine is None`, each
# build a full engine + connection pool via `build_engine()`, and the loser's engine (and its
# pool/connections) would be silently orphaned rather than reused. The lock is only ever held for
# the one-time duration of building the engine, not per-request.
_engine_lock = threading.Lock()


def _get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:  # re-check: another thread may have built it while we waited
                _engine = build_engine()
    return _engine


def _reset_engine_cache() -> None:
    """Test-only: force the next `_get_engine()` call to rebuild against the current env.

    Disposes the outgoing engine's connection pool before dropping the reference -- otherwise its
    connections leak (for file-based SQLite this can surface as "database is locked" flakiness in
    a subsequent test; for a pooled backend like PostgreSQL, connections would accumulate across
    every test that resets this cache).
    """
    global _engine
    with _engine_lock:
        old_engine = _engine
        _engine = None
    if old_engine is not None:
        old_engine.dispose()


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency: yields a `Session` on the shared engine, closed after the request."""
    with Session(_get_engine()) as session:
        yield session


def get_llm_provider() -> LLMProvider:
    """FastAPI dependency: a real `LLMProvider` resolved the same way `cli.py` does (R16/R17 --
    never a concrete SDK directly), for the one route so far that needs to compose text on demand
    (`api/document.py`'s additional-info draft route).

    A plain `Depends(...)` callable, not the `get_session()`-style lazily-cached module global
    above: `build_llm_provider()` is cheap (env/config-file reads only, no network call itself --
    the adapter it returns is what later makes an HTTP call, on `.complete()`), and this indirection
    exists purely so a test can override it via `app.dependency_overrides[get_llm_provider]` with a
    `MockLLMProvider()`, exactly like `get_session` is overridden with a test `Session` -- there is
    no other way to substitute a fake LLM into a route this module doesn't itself construct one
    for.
    """
    return build_llm_provider()


def get_llm_model() -> str:
    """FastAPI dependency: the real, resolved model name for `get_llm_provider()`'s adapter (e.g.
    `resolve_llm_config().model`) -- separate from `get_llm_provider()` itself (rather than bundled
    into one dependency returning a `(provider, model)` pair) purely so a test can override just
    this one with a fixed string via `app.dependency_overrides[get_llm_model]`, independent of
    whatever fake `LLMProvider` it also injects via `get_llm_provider` -- mirrors `cli.py`'s
    existing "resolve twice, cheap, no I/O beyond env/file reads" pattern for the identical reason
    (`_build_jurisdiction_detector()`/`_build_response_classifier()`, `main_async()`'s own
    `llm_config.model`)."""
    return resolve_llm_config().model


def get_jurisdiction_detector(
    llm: LLMProvider = Depends(get_llm_provider),
) -> JurisdictionDetector:
    """FastAPI dependency: a real `JurisdictionDetector`, for `api/add_case.py`'s kanban
    add-a-case route (issue #125's kanban half).

    A duplicate of `cli.py`'s `_build_jurisdiction_detector()`, not an import of it -- this one is
    small and cheap to duplicate, so it follows this codebase's established convention of
    duplicating small cross-module helpers rather than creating a backwards `api/` -> `cli.py`
    dependency for something this narrow (e.g. `case_actions._has_fresh_text_response()`
    duplicating `followup._pending_text_response()`'s staleness shape). Contrast `api/add_case.py`,
    which *does* import `cli.add_case_for_url()` directly -- see that module's own docstring for
    why the calculus flips for a larger, real piece of logic. Same behavior as this function's
    `cli.py` counterpart: crawling the
    site itself needs no external service (`HTTPWebFetchProvider`, unconditional); SearXNG is only
    an optional fallback, so `resolve_searxng_config()` raising `LookupError` (no `[websearch]`
    configured) just means `websearch=None`, not "detection off" -- only a genuine misconfiguration
    of an otherwise-present `[websearch]` section still raises.
    """
    websearch: WebSearchProvider | None
    try:
        websearch = SearXNGProvider(resolve_searxng_config())
    except LookupError:
        websearch = None

    llm_config = resolve_llm_config()
    return JurisdictionDetector(
        websearch,
        llm,
        model=llm_config.model,
        webfetch=HTTPWebFetchProvider(),
    )
