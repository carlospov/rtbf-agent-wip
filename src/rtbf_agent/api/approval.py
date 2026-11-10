"""Approve/reject routes for the kanban web UI (issue #67) -- a second approval channel alongside
Telegram, not a replacement for it.

**The actual architecture change (read `agent_loop.send_request()`'s docstring for the CLI-side
half):** this API process and the `python -m rtbf_agent` CLI process are separate processes with
no shared in-memory state -- a human clicking "Approve" here has no `Notifier`-style `Future` to
resolve the way `TelegramNotifier`'s callback-query handler does. So this module does the only
thing an API process safely can: for a `Case` actually in `Draft ready`, it records the exact same
`approval_granted`/`approval_rejected` `Event` shape `agent_loop.send_request()` already writes --
but never calls a `Sender` itself (this process has no SMTP wiring, and shouldn't need one just to
record a decision). The recurring loop's next pass (via
`agent_loop._existing_approval_granted_event()`) finds that `Event` already there and proceeds
straight to sending, instead of soliciting a fresh decision through whatever `Notifier` is
configured.

**The actual decision-recording logic lives in `case_actions.py`** (`approve_draft()`/
`reject_draft()`), shared with the CLI's equivalent subcommands and the same gate/`Event` shapes
this module always used -- this file is now just the HTTP adapter: parse, call, translate
`case_actions`' plain exceptions to the matching `HTTPException` + i18n string.

**R23:** every user-facing string this module returns is sourced from `I18nProvider.translate()`,
same invariant `kanban.py`/`case_detail.py` already enforce, checked the same mechanical way (see
`tests/api/test_approval.py`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from rtbf_agent.api.deps import get_session
from rtbf_agent.case_actions import (
    CaseNotFoundError,
    InvalidCaseStateError,
    approve_draft,
    reject_draft,
)
from rtbf_agent.config import resolve_interface_language
from rtbf_agent.core.interfaces import I18nProvider
from rtbf_agent.plugins.i18n.resource import ResourceI18nProvider

router = APIRouter()

# One `I18nProvider` instance for the whole process, mirroring `kanban.py`/`case_detail.py`.
_i18n: I18nProvider = ResourceI18nProvider()


def _not_found_or_wrong_state(exc: CaseNotFoundError | InvalidCaseStateError) -> HTTPException:
    """Translate a `case_actions` gating failure to this route's existing 404/409 + i18n shape --
    unchanged from before `case_actions.py` existed, just derived from the exception's structured
    attributes instead of computed inline."""
    lang = resolve_interface_language()
    if isinstance(exc, CaseNotFoundError):
        return HTTPException(status_code=404, detail=_i18n.translate("approval.not_found", lang))
    return HTTPException(
        status_code=409,
        detail=_i18n.translate(
            "approval.wrong_state", lang, state=exc.state.value, case_id=exc.case_id
        ),
    )


@router.post("/cases/{case_id:int}/approve")
def approve_case(case_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    """Record an `approval_granted` `Event` for a `Draft ready` `Case` -- never sends (issue #67).

    `case.state` is deliberately left at `Draft ready` here -- only a successful send, once the
    recurring loop actually performs it, ever moves it onward (R1: this route records a decision,
    it does not perform the irreversible action itself).
    """
    try:
        approve_draft(session, case_id)
    except (CaseNotFoundError, InvalidCaseStateError) as exc:
        raise _not_found_or_wrong_state(exc) from exc
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)


@router.post("/cases/{case_id:int}/reject")
def reject_case(case_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    """Reject a `Draft ready` `Case` -- immediate and final: `case.state` moves to
    `Rejected/Escalated` right here, since there's no further step for anything to take on a
    rejected case.
    """
    try:
        reject_draft(session, case_id)
    except (CaseNotFoundError, InvalidCaseStateError) as exc:
        raise _not_found_or_wrong_state(exc) from exc
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)
