"""Review/override a case's applicable jurisdiction or drafting language from the kanban web UI --
the web-actionable counterpart of the CLI's `set-jurisdiction`/`set-drafting-language` subcommands.

**Why this exists:** a later maintainer decision reversed issue #43's original "prompt at case
creation" design (see `cli.prompt_case_overrides()`'s removal, `cli._apply_jurisdiction_
proposal()`): no case creation path prompts a human anymore -- a detected jurisdiction suggestion
is auto-applied (or the field is left `None`, deferring to the global default) with zero
interaction. This module is the new review/override path that replaces the old prompt: a human can
still change either field, but only while the case is still `Discovered` -- before `agent_loop.
_classify()` ever reads and locks them in for the round (`case_actions.set_case_jurisdiction()`/
`set_case_drafting_language()`'s own docstrings cover the exact gate).

**The actual decision-recording logic lives in `case_actions.py`**, shared with the CLI
subcommands -- this file is just the HTTP adapter: parse form, call, translate `case_actions`'
plain exceptions to the matching `HTTPException` + i18n string, mirroring `draft.py`/`recipient.
py`'s established pattern exactly (`_not_found_or_wrong_state()` for 404/409, a dedicated 400 for
`InvalidInputError`).

Two independent routes, not one combined form (R22: jurisdiction/drafting-language are independent
axes throughout this codebase, never a package deal) -- mirrors `case_detail.html`'s existing
per-field-independent forms (e.g. the recipient-confirm form, the text-response form).

**R23:** every user-facing string this module returns is sourced from `I18nProvider.translate()`,
same invariant every other `api/` module already enforces, checked the same mechanical way (see
`tests/api/test_case_overrides.py`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from rtbf_agent import case_actions
from rtbf_agent.api.deps import get_session
from rtbf_agent.case_actions import CaseNotFoundError, InvalidCaseStateError, InvalidInputError
from rtbf_agent.config import resolve_interface_language
from rtbf_agent.core.interfaces import I18nProvider
from rtbf_agent.plugins.i18n.resource import ResourceI18nProvider

router = APIRouter()

# One `I18nProvider` instance for the whole process, mirroring every other `api/` module.
_i18n: I18nProvider = ResourceI18nProvider()


def _not_found_or_wrong_state(exc: CaseNotFoundError | InvalidCaseStateError) -> HTTPException:
    """Translate a `case_actions` gating failure to this route's 404/409 + i18n shape -- mirrors
    `draft.py`/`approval.py`'s `_not_found_or_wrong_state()` exactly."""
    lang = resolve_interface_language()
    if isinstance(exc, CaseNotFoundError):
        return HTTPException(
            status_code=404, detail=_i18n.translate("case_overrides.not_found", lang)
        )
    return HTTPException(
        status_code=409,
        detail=_i18n.translate("case_overrides.wrong_state", lang, case_id=exc.case_id),
    )


@router.post("/cases/{case_id:int}/jurisdiction")
def set_jurisdiction(
    case_id: int, jurisdiction: str = Form(...), session: Session = Depends(get_session)
) -> RedirectResponse:
    """Set `Case.applicable_jurisdiction` for a still-`Discovered` `Case`.

    Plain `def`, not `async def` (matches `draft.py`/`recipient.py`'s routes): the handler does
    nothing but a synchronous `case_actions` call.
    """
    lang = resolve_interface_language()
    try:
        case_actions.set_case_jurisdiction(session, case_id, jurisdiction)
    except (CaseNotFoundError, InvalidCaseStateError) as exc:
        raise _not_found_or_wrong_state(exc) from exc
    except InvalidInputError as exc:
        raise HTTPException(
            status_code=400, detail=_i18n.translate("case_overrides.invalid_jurisdiction", lang)
        ) from exc
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)


@router.post("/cases/{case_id:int}/drafting-language")
def set_drafting_language(
    case_id: int, drafting_language: str = Form(...), session: Session = Depends(get_session)
) -> RedirectResponse:
    """Set `Case.drafting_language` for a still-`Discovered` `Case`. See `set_jurisdiction()`'s
    docstring above -- same gate, same error shape, deliberately independent (R22)."""
    lang = resolve_interface_language()
    try:
        case_actions.set_case_drafting_language(session, case_id, drafting_language)
    except (CaseNotFoundError, InvalidCaseStateError) as exc:
        raise _not_found_or_wrong_state(exc) from exc
    except InvalidInputError as exc:
        raise HTTPException(
            status_code=400,
            detail=_i18n.translate("case_overrides.invalid_drafting_language", lang),
        ) from exc
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)
