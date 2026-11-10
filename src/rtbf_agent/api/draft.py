"""Edit a `Draft ready` case's drafted body from the kanban web UI (LLM-drafted-replies extension)
-- the web-actionable counterpart of the CLI's `edit-draft` subcommand.

**The actual decision-recording logic lives in `case_actions.py`** (`edit_draft()`), shared with
that CLI subcommand and the same gate/`Event` shape this module always uses -- this file is just
the HTTP adapter: parse form, call `case_actions.edit_draft()`, catch/translate its plain
exceptions to the matching `HTTPException` + i18n string, mirroring `approval.py`/`document.py`'s
established pattern exactly.

**Records only** -- `edit_draft()` never touches `case.state`, and never calls an LLM (see its own
docstring): it records exactly the text the human submitted, verbatim. The recurring loop's next
pass sends whatever the latest `draft_proposed` `Event` holds once a human approves it, same as any
other draft.

**R23:** every user-facing string this module returns is sourced from `I18nProvider.translate()`,
same invariant every other `api/` module already enforces, checked the same mechanical way (see
`tests/api/test_draft.py`).
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

# One `I18nProvider` instance for the whole process, mirroring `approval.py`/`document.py`.
_i18n: I18nProvider = ResourceI18nProvider()


def _not_found_or_wrong_state(exc: CaseNotFoundError | InvalidCaseStateError) -> HTTPException:
    """Translate a `case_actions` gating failure to this route's 404/409 + i18n shape -- mirrors
    `approval.py`'s `_not_found_or_wrong_state()` exactly."""
    lang = resolve_interface_language()
    if isinstance(exc, CaseNotFoundError):
        return HTTPException(status_code=404, detail=_i18n.translate("draft.not_found", lang))
    return HTTPException(
        status_code=409,
        detail=_i18n.translate("draft.wrong_state", lang, case_id=exc.case_id),
    )


@router.post("/cases/{case_id:int}/edit-draft")
def edit_draft(
    case_id: int, body: str = Form(...), session: Session = Depends(get_session)
) -> RedirectResponse:
    """Record a hand-edited draft body for a `Draft ready`, not-yet-approved `Case`.

    Plain `def`, not `async def` (matches `approval.py`/`document.py`'s routes): the handler does
    nothing but a synchronous `case_actions` call.
    """
    lang = resolve_interface_language()
    try:
        case_actions.edit_draft(session, case_id, body)
    except (CaseNotFoundError, InvalidCaseStateError) as exc:
        raise _not_found_or_wrong_state(exc) from exc
    except InvalidInputError as exc:
        raise HTTPException(
            status_code=400, detail=_i18n.translate("draft.invalid_body", lang)
        ) from exc
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)
