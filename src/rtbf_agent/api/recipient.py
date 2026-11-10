"""Recipient-confirmation route for the kanban web UI -- makes the deletion-request recipient a
first-class, web-actionable field, not CLI-only (real report).

**Why this needs its own route, not a tweak to `approval.py`:** approving/rejecting decides
whether to send an already-drafted request; confirming the recipient decides *who* that request
goes to -- a different, earlier decision in the same `Draft ready` window. `agent_loop.
send_request()`/`_execute_send()` already refuse to send with no `Case.recipient_email` set (R1's
un-bypassable backstop) -- this route just gives a human a channel to make that decision, mirroring
`approval.py`'s "a second channel alongside Telegram/CLI, not a replacement" framing exactly.

**The actual decision-recording logic lives in `case_actions.py`** (`confirm_recipient()`), shared
with the CLI's `confirm-recipient` subcommand -- this file is now just the HTTP adapter: parse,
call, translate `case_actions`' plain exceptions to the matching `HTTPException` + i18n string.

**Unlike approve (idempotent-by-`Event`-only) or reject (terminal), this route can be resubmitted**
while the case remains `Draft ready` *and not yet approved* -- confirming *or correcting* a typo
before approving is a legitimate, repeatable action, not a one-shot decision. See
`case_actions.confirm_recipient()`'s docstring and its `_update_recipient_if_not_approved()` helper
for the atomic-update TOCTOU discussion (R1, code-review finding from this route's original PR)
that still applies verbatim now that the logic lives there.

**R23:** every user-facing string this module returns is sourced from `I18nProvider.translate()`,
same invariant `approval.py`/`case_detail.py` already enforce, checked the same mechanical way (see
`tests/api/test_recipient.py`).
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
from rtbf_agent.core.models import CaseState
from rtbf_agent.plugins.i18n.resource import ResourceI18nProvider

router = APIRouter()

# One `I18nProvider` instance for the whole process, mirroring `approval.py`/`case_detail.py`.
_i18n: I18nProvider = ResourceI18nProvider()


@router.post("/cases/{case_id:int}/recipient")
def confirm_recipient(
    case_id: int, recipient_email: str = Form(...), session: Session = Depends(get_session)
) -> RedirectResponse:
    """Set `Case.recipient_email` for a `Draft ready` `Case` and record a `recipient_confirmed`
    `Event`.

    `recipient_email` comes straight from the case-detail page's form (pre-filled with the current
    value, or the latest suggestion, or blank) -- submitting it unedited is the low-friction
    "accept" path, editing it first is the override path. `400` for a blank or `@`-less value --
    catches the most common mistake immediately rather than letting it reach the database and fail
    much later at the SMTP layer. `409` once the case is already approved (see
    `case_actions.confirm_recipient()`'s docstring) or not in `Draft ready` at all.
    """
    lang = resolve_interface_language()
    try:
        case_actions.confirm_recipient(session, case_id, recipient_email)
    except CaseNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=_i18n.translate("approval.not_found", lang)
        ) from exc
    except InvalidInputError as exc:
        raise HTTPException(
            status_code=400, detail=_i18n.translate("recipient.invalid_address", lang)
        ) from exc
    except InvalidCaseStateError as exc:
        # Two distinct sub-cases share this exception (see confirm_recipient()'s docstring): the
        # case was never in Draft ready at all (message names its actual state, mirroring
        # approval.py's wrong-state message), vs. it was Draft ready but already approved for send
        # (message names that specifically, since "approved" is a narrower, more useful fact than
        # "still Draft ready"). Discriminated on the exception's structured `state` attribute, not
        # `expected`'s wording (OCR review finding) -- robust against a future reword of
        # `_draft_ready_case()`'s message, unlike a string comparison against it.
        if exc.state != CaseState.DRAFT_READY:
            detail = _i18n.translate(
                "approval.wrong_state", lang, state=exc.state.value, case_id=case_id
            )
        else:
            detail = _i18n.translate("recipient.already_approved", lang, case_id=case_id)
        raise HTTPException(status_code=409, detail=detail) from exc
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)
