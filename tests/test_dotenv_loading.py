"""Regression tests for issue #84: `.env` must actually reach `os.environ`, for both real
entrypoints (`python -m rtbf_agent`/`rtbf-agent`/`forget`, and `uvicorn rtbf_agent.api:app`).

Before this issue, nothing in this codebase ever called `python-dotenv`'s `load_dotenv()` --
`RTBF_TARGET_EMAIL`/`DATABASE_URL`/etc. in a `.env` file were silently invisible to every config
resolver, which all read straight from `os.environ`. `rtbf_agent.__main__` and
`rtbf_agent.api.app` are the two modules a real invocation actually imports first (see each
module's own docstring for why there's no third, shared hook point), so those are the two call
sites checked here.

These tests check *wiring* (each entrypoint module calls `load_dotenv()` at import time) rather
than `python-dotenv`'s own directory-search algorithm (`find_dotenv()`) -- that's an
already-tested third-party behavior, not this codebase's to re-verify. The override-semantics test
below does directly exercise the actual call shape (`load_dotenv()`, no keyword arguments -> the
library's own `override=False` default) against a controlled, explicit `dotenv_path`, which is the
one behavioral guarantee (never clobber an already-exported real env var) this issue's fix
actually depends on.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest


def _reload_fresh(module_name: str) -> Iterator[None]:
    """Force a module to re-execute its top-level code (including `load_dotenv()`) on next import.

    Every test below imports at least one of `rtbf_agent.__main__`/`rtbf_agent.api.app` -- both of
    which other test modules may have already imported (and Python caches in `sys.modules`), so a
    plain `import` here would silently no-op instead of re-running the module's top-level
    `load_dotenv()` call. Tears down by dropping the module from the cache again afterward, so a
    later test importing it for real (not through this helper) gets a genuine, un-mocked reload
    rather than the mock-bound version this test left behind.
    """
    sys.modules.pop(module_name, None)
    yield
    sys.modules.pop(module_name, None)


@pytest.fixture()
def fresh_main_module() -> Iterator[None]:
    yield from _reload_fresh("rtbf_agent.__main__")


@pytest.fixture()
def fresh_api_app_module() -> Iterator[None]:
    yield from _reload_fresh("rtbf_agent.api.app")


def test_main_module_loads_dotenv_at_import_time(
    monkeypatch: pytest.MonkeyPatch, fresh_main_module: None
) -> None:
    calls: list[object] = []
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: calls.append((a, k)) or True)

    importlib.import_module("rtbf_agent.__main__")

    assert len(calls) == 1


def test_api_app_module_loads_dotenv_at_import_time(
    monkeypatch: pytest.MonkeyPatch, fresh_api_app_module: None
) -> None:
    calls: list[object] = []
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: calls.append((a, k)) or True)

    importlib.import_module("rtbf_agent.api.app")

    assert len(calls) == 1


def test_dotenv_load_never_overrides_an_already_exported_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exact call our entrypoints make (`load_dotenv()`, no keyword arguments) must never let
    a `.env` file's value clobber a real, already-exported environment variable -- a shell-set
    value always wins, `.env` is only ever a fallback default."""
    import dotenv

    env_file = tmp_path / ".env"
    env_file.write_text("RTBF_TEST_ONLY_DOTENV_VAR=from-dotenv-file\n")
    monkeypatch.setenv("RTBF_TEST_ONLY_DOTENV_VAR", "from-real-environment")

    dotenv.load_dotenv(dotenv_path=env_file)

    assert os.environ["RTBF_TEST_ONLY_DOTENV_VAR"] == "from-real-environment"


def test_dotenv_load_fills_in_an_unset_var_from_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The complementary case: with nothing already exported, `.env`'s value is what's used --
    proving the fallback direction actually works, not just that it's harmless when unused."""
    import dotenv

    env_file = tmp_path / ".env"
    env_file.write_text("RTBF_TEST_ONLY_DOTENV_VAR=from-dotenv-file\n")
    monkeypatch.delenv("RTBF_TEST_ONLY_DOTENV_VAR", raising=False)

    dotenv.load_dotenv(dotenv_path=env_file)

    assert os.environ["RTBF_TEST_ONLY_DOTENV_VAR"] == "from-dotenv-file"
