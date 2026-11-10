"""Add a case for a site you already know about, from the kanban web UI (issue #125's kanban half).

The web-actionable counterpart of the shell's `add <url>` command -- both call the exact same
`cli.add_case_for_url()` (issue #66), so the `(domain, target_email)` dedup key, jurisdiction
auto-apply (issue #63 parity, `api/deps.get_jurisdiction_detector()`), and `Case` construction are
all shared verbatim, never a second case-creation code path. This route does not run the
Gmail-registration cross-check the old standalone `add-case` subcommand used to (that subcommand
was removed entirely, per a later maintainer decision -- see `cli.py`'s `shell_main()` docstring) --
same as the shell's own `add`, which never ran it either.

**Why a separate module, not folded into `api/kanban.py`:** `kanban.py`'s own module docstring
frames it as "the whole of the kanban board's server-side surface -- route, query, and i18n
resolution", already scoped narrowly to the `GET /cases` board render. This route is a mutation
(creates a `Case`), matching the shape every other case-mutating route in this app already gets its
own module for (`approval.py`, `recipient.py`, `case_overrides.py`, `followup.py`).

**Importing `add_case_for_url()` from `cli.py`, not duplicating it:** unlike
`_resolve_target_email()` below (a 6-line helper, cheap to duplicate) or `api/deps.
get_jurisdiction_detector()` (duplicated to avoid a backwards `api/` -> `cli.py` dependency for
something small), `add_case_for_url()` itself is real, non-trivial case-creation logic -- dedup,
`DiscoveredAccount` construction, jurisdiction auto-apply, the optional registration cross-check.
Duplicating *that* would risk the two paths silently drifting apart over time; a single shared
implementation is worth the one narrow `api/` -> `cli.py` import (no circular import: `cli.py`
never imports anything from `api/`). `RTBF_TARGET_EMAIL` itself (the env var this needs) is still
resolved via a tiny duplicated `_resolve_target_email()` below, rather than importing `cli.py`'s
private one, since that part genuinely is cheap to duplicate.

**R23:** every user-facing string this module returns is sourced from `I18nProvider.translate()`,
checked the same mechanical way every other `api/` module already is (see
`tests/api/test_add_case.py`).
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from rtbf_agent.api.deps import get_jurisdiction_detector, get_session
from rtbf_agent.cli import add_case_for_url
from rtbf_agent.config import resolve_interface_language
from rtbf_agent.core.interfaces import I18nProvider
from rtbf_agent.plugins.i18n.resource import ResourceI18nProvider
from rtbf_agent.plugins.jurisdiction_detection.detector import JurisdictionDetector

router = APIRouter()

# One `I18nProvider` instance for the whole process, mirroring every other `api/` module.
_i18n: I18nProvider = ResourceI18nProvider()


def _resolve_target_email() -> str:
    """Duplicate of `cli._resolve_target_email()` -- see this module's own docstring for why this
    is a duplicate, not an import."""
    target_email = os.environ.get("RTBF_TARGET_EMAIL")
    if not target_email:
        raise LookupError(
            "no target email configured: set RTBF_TARGET_EMAIL to the mailbox address to scan"
        )
    return target_email


@router.post("/cases")
async def add_case(
    url: str = Form(...),
    session: Session = Depends(get_session),
    jurisdiction_detector: JurisdictionDetector = Depends(get_jurisdiction_detector),
) -> RedirectResponse:
    """Create (or dedupe to an existing) `Case` for `url`, then redirect to its detail page.

    `async def`, unlike this app's plain-`def` mutation routes (`approval.py`/`recipient.py`/
    `case_overrides.py`) -- `add_case_for_url()` itself is `async` (it may call the injected
    `jurisdiction_detector`'s real HTTP-fetching/LLM-calling `propose()`), mirroring `document.py`'s
    `draft_additional_info` route, the one other route in this app that awaits real I/O.

    A blank/unparseable `url` (`extract_domain()` raises `ValueError`) or a missing
    `RTBF_TARGET_EMAIL` (`_resolve_target_email()` raises `LookupError`) both become a 400 -- the
    former is genuinely bad user input, the latter is a server misconfiguration, but neither should
    surface as an unhandled 500 with a raw traceback.
    """
    lang = resolve_interface_language()
    try:
        target_email = _resolve_target_email()
    except LookupError as exc:
        raise HTTPException(
            status_code=400, detail=_i18n.translate("add_case.no_target_email", lang)
        ) from exc
    try:
        case = await add_case_for_url(
            session, url, target_email, jurisdiction_detector=jurisdiction_detector
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=_i18n.translate("add_case.invalid_url", lang)
        ) from exc
    return RedirectResponse(url=f"/cases/{case.id}", status_code=303)
