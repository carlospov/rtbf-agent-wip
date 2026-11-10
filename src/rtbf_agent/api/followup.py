"""Confirm/dispute a classified follow-up reply from the kanban web UI (Phase 5 of the
automated-case-loop rework) -- the web-actionable counterpart of the CLI's `confirm-followup`/
`dispute-followup` subcommands.

**The actual decision-recording logic lives in `case_actions.py`** (`confirm_followup()`/
`dispute_followup()`), shared with those CLI subcommands and the same gate/`Event` shapes this
module always used -- this file is just the HTTP adapter: parse, call, translate `case_actions`'
plain exceptions to the matching `HTTPException` + i18n string, mirroring `approval.py`/
`recipient.py`'s established pattern exactly.

Confirming closes the case (`Rejected/Escalated`, for the only intent that can still be pending
here -- see below) immediately -- there is no further "next pass sends it" step the way approving a
draft has, since confirming *is* the terminal action here. Disputing moves the case to
`Requires human action` instead of any terminal state, so a human never auto-closes a case just
because the agent misread a reply.

**`CONFIRMATION` no longer reaches this router's `confirm`/`dispute` routes at all** (maintainer-
directed narrowing of issue #68 -- see `followup.py`'s module docstring, "Why CONFIRMATION
auto-closes now"): `followup.follow_up()` now resolves a `CONFIRMATION` classification immediately,
itself, closing the case as `Deletion confirmed` with no pending decision left for a human to act
on via these two routes. This module's `reopen` route below is the mitigating control for a wrong
auto-close -- not a variant of confirm/dispute, but an explicit undo back to
`Requires human action`.

**R23:** every user-facing string this module returns is sourced from `I18nProvider.translate()`,
same invariant `approval.py`/`recipient.py`/`case_detail.py` already enforce, checked the same
mechanical way (see `tests/api/test_followup.py`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from rtbf_agent import case_actions
from rtbf_agent.api.deps import get_session
from rtbf_agent.case_actions import CaseNotFoundError, InvalidCaseStateError
from rtbf_agent.config import resolve_interface_language
from rtbf_agent.core.interfaces import I18nProvider
from rtbf_agent.plugins.i18n.resource import ResourceI18nProvider

router = APIRouter()

# One `I18nProvider` instance for the whole process, mirroring `approval.py`/`recipient.py`.
_i18n: I18nProvider = ResourceI18nProvider()


def _not_found_or_wrong_state(exc: CaseNotFoundError | InvalidCaseStateError) -> HTTPException:
    """Translate a `case_actions` gating failure to this route's 404/409 + i18n shape -- mirrors
    `approval.py`'s `_not_found_or_wrong_state()` exactly."""
    lang = resolve_interface_language()
    if isinstance(exc, CaseNotFoundError):
        return HTTPException(status_code=404, detail=_i18n.translate("approval.not_found", lang))
    return HTTPException(
        status_code=409,
        detail=_i18n.translate("followup.wrong_state", lang, case_id=exc.case_id),
    )


@router.post("/cases/{case_id:int}/followup/confirm")
def confirm_followup(case_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    """Confirm a classified reply's outcome -- closes the case as `Deletion confirmed`/
    `Rejected/Escalated` (whichever intent was classified) right here; there is no further pass
    for anything else to do."""
    try:
        case_actions.confirm_followup(session, case_id)
    except (CaseNotFoundError, InvalidCaseStateError) as exc:
        raise _not_found_or_wrong_state(exc) from exc
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)


@router.post("/cases/{case_id:int}/followup/dispute")
def dispute_followup(case_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    """Dispute a classified reply's outcome -- moves the case to `Requires human action` instead
    of any terminal state, since the human disagrees with the agent's reading."""
    try:
        case_actions.dispute_followup(session, case_id)
    except (CaseNotFoundError, InvalidCaseStateError) as exc:
        raise _not_found_or_wrong_state(exc) from exc
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)


@router.post("/cases/{case_id:int}/followup/reopen")
def reopen_case(case_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    """Undo a wrongly auto-closed `Deletion confirmed` case -- moves it back to
    `Requires human action` so a human can act on it (the mitigating control for
    `follow_up()`'s `CONFIRMATION` auto-close; see `case_actions.reopen_case()`'s docstring).
    Refuses (409) for any other state -- reusing the same 404/409 translation shape (and the same
    `followup.wrong_state` i18n string) as `confirm`/`dispute` above."""
    try:
        case_actions.reopen_case(session, case_id)
    except (CaseNotFoundError, InvalidCaseStateError) as exc:
        raise _not_found_or_wrong_state(exc) from exc
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)
