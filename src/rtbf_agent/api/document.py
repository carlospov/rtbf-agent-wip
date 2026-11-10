"""Supply additional info (text or a document) for a `Requires human action` case, from the
kanban web UI (Phase 5 of the automated-case-loop rework) -- the web-actionable counterpart of the
CLI's `provide-text`/`attach-document`/`draft-additional-info` subcommands. Also the human's
explicit "compose a preview draft from what's on record" action (`POST .../additional-info/draft`
below), a deliberately separate step from either of those, and a deliberately synchronous *preview*
step, not a send -- see `followup.py`'s module docstring, "Why drafting is now an explicit,
synchronous action", for why this superseded issue #117/#119's fused, poll-triggered draft+send.

**The actual decision-recording logic lives in `case_actions.py`** (`provide_text_response()`/
`attach_document()`) for the two record-only routes, shared with the CLI subcommands and the same
gate/`Event`/`Document`-lifecycle shapes this module always used -- this file is just the HTTP
adapter: parse, call, translate `case_actions`' plain exceptions to the matching `HTTPException` +
i18n string, mirroring `approval.py`/`recipient.py`/`followup.py`'s established pattern exactly.
The draft route below instead calls `followup.draft_additional_info_response()` directly (it needs
a real `LLMProvider` to compose the reply body, not just DB-only decision recording), translating
its `ValueError` preconditions to the same 404/409/400 + i18n shape.

**Records only, never sends here** -- see `case_actions.provide_text_response()`/
`attach_document()`'s own docstrings for why the actual resend is a separate, later, explicit
human action (`case_actions.approve_draft()`, once a preview draft exists), never this route's.

**R23:** every user-facing string this module returns is sourced from `I18nProvider.translate()`,
same invariant every other `api/` module already enforces, checked the same mechanical way (see
`tests/api/test_document.py`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from rtbf_agent import case_actions
from rtbf_agent.api.deps import get_llm_model, get_llm_provider, get_session
from rtbf_agent.case_actions import CaseNotFoundError, InvalidCaseStateError, InvalidInputError
from rtbf_agent.config import resolve_interface_language
from rtbf_agent.core.interfaces import I18nProvider, LLMProvider
from rtbf_agent.core.models import Case, CaseState
from rtbf_agent.followup import draft_additional_info_response
from rtbf_agent.plugins.i18n.resource import ResourceI18nProvider

router = APIRouter()

# One `I18nProvider` instance for the whole process, mirroring `approval.py`/`recipient.py`.
_i18n: I18nProvider = ResourceI18nProvider()

_MAX_DOCUMENT_BYTES = 10 * 1024 * 1024  # 10 MiB -- an identity-document scan/photo, not a video.
"""Upper bound on an uploaded document's size (OCR review finding): unbounded `UploadFile.read()`
would let a client make this process allocate/encrypt/write arbitrarily large content, a real
memory-exhaustion/disk-fill vector even for a single-operator tool exposed on a local network."""


def _not_found_or_wrong_state(exc: CaseNotFoundError | InvalidCaseStateError) -> HTTPException:
    """Translate a `case_actions` gating failure to this route's 404/409 + i18n shape -- mirrors
    `approval.py`'s `_not_found_or_wrong_state()` exactly."""
    lang = resolve_interface_language()
    if isinstance(exc, CaseNotFoundError):
        return HTTPException(status_code=404, detail=_i18n.translate("approval.not_found", lang))
    return HTTPException(
        status_code=409,
        detail=_i18n.translate("additional_info.wrong_state", lang, case_id=exc.case_id),
    )


@router.post("/cases/{case_id:int}/text-response")
def provide_text_response(
    case_id: int, text: str = Form(...), session: Session = Depends(get_session)
) -> RedirectResponse:
    """Record a text answer for a `Requires human action` case.

    Plain `def`, not `async def` (OCR review finding): this handler does nothing but a
    synchronous `case_actions` call (itself plain SQLAlchemy DB I/O) -- FastAPI runs a plain `def`
    route in its worker thread pool rather than directly on the event loop, matching
    `approval.py`/`recipient.py`/`followup.py`'s established pattern for the exact same reason.
    """
    lang = resolve_interface_language()
    try:
        case_actions.provide_text_response(session, case_id, text)
    except (CaseNotFoundError, InvalidCaseStateError) as exc:
        raise _not_found_or_wrong_state(exc) from exc
    except InvalidInputError as exc:
        raise HTTPException(
            status_code=400, detail=_i18n.translate("additional_info.invalid_text", lang)
        ) from exc
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)


@router.post("/cases/{case_id:int}/additional-info/draft")
async def draft_additional_info(
    case_id: int,
    session: Session = Depends(get_session),
    llm: LLMProvider = Depends(get_llm_provider),
    model: str = Depends(get_llm_model),
) -> RedirectResponse:
    """Compose a preview draft from the current `Requires human action` round's text/document
    answer -- the human's single, explicit "Draft" action, distinct from (and usable any time
    after) recording a text/document answer via the two routes above. `case.state` stays `Requires
    human action`; nothing is sent (the "drafting is a preview, not a transition" redesign that
    superseded issue #117/#119's fused, poll-triggered draft+send -- see `followup.py`'s module
    docstring). The case-detail page's now-widened `can_edit_draft`/`can_approve` (`api/
    case_detail.py`) render the same review/edit/approve/reject UI a first-round draft already
    gets, without the case ever appearing to change kanban column.

    **The actual compose logic lives in `followup.draft_additional_info_response()`** (resolving the
    human's current fresh text/document answer) -> `agent_loop.compose_additional_info_draft()`
    (the LLM call + `draft_proposed` `Event`) -- this route is the HTTP adapter: resolve the case,
    inject a real `LLMProvider`/model (R16/R17 -- never a concrete SDK directly; `api/deps.py`'s
    `get_llm_provider()`/`get_llm_model()`, shared with `cli.py`'s `draft-additional-info`
    subcommand's own resolution logic rather than duplicated, and overridable in tests the same way
    `get_session` already is), call, commit, translate a precondition failure to this route's
    404/409/400 + i18n shape.

    `async def`, not the `def` every sibling route in this file uses (OCR review finding pattern
    reversed on purpose): every other route here does nothing but synchronous DB/disk I/O, which
    FastAPI runs in its worker thread pool -- this route's actual work is an `await`ed LLM HTTP
    call, so it runs directly on the event loop like every other async collaborator call in this
    codebase (`agent_loop`/`followup`), not blocking a thread-pool worker on it instead.
    """
    lang = resolve_interface_language()
    case = session.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=_i18n.translate("approval.not_found", lang))
    if case.state != CaseState.REQUIRES_HUMAN_ACTION:
        raise HTTPException(
            status_code=409,
            detail=_i18n.translate("additional_info.wrong_state", lang, case_id=case_id),
        )
    try:
        await draft_additional_info_response(session, case, llm, model=model)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=_i18n.translate("additional_info.nothing_to_draft", lang)
        ) from exc
    session.commit()
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)


@router.post("/cases/{case_id:int}/document")
def attach_document(
    case_id: int,
    documents: list[UploadFile],
    label: str = Form(default=""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Record one or more document answers for a `Requires human action` case, through the same
    encrypted-at-rest lifecycle every other document channel uses (`case_actions.attach_document()`
    -> `document_store.store_document()`).

    Plain `def`, not `async def` (OCR review finding): reading `document.file` (a `SpooledTemporary
    File`, not a coroutine) and calling `case_actions.attach_document()` (SQLAlchemy I/O, Fernet
    encryption, disk I/O) are both synchronous work -- a plain `def` route runs in FastAPI's worker
    thread pool rather than blocking the event loop directly, matching every other case-action
    route in this codebase. Rejects anything over `_MAX_DOCUMENT_BYTES` per file (OCR review
    finding): an unbounded read would let a client make this process allocate/encrypt/write
    arbitrarily large content.

    Multi-file upload (live-UX follow-up to issue #111): `documents` replaces the old single
    `document` field so attaching an ID front + back + a proof-of-address takes one round-trip, not
    three. Whole-request-fails-together semantics: every file's bytes are read and size/emptiness-
    checked in one upfront pass, *before* `case_actions.attach_document()` (which commits per call
    -- there is no single enclosing transaction to roll back) is ever called for any of them -- so
    an oversized or empty file anywhere in the batch fails the whole request with none recorded,
    rather than leaving the files before it already persisted. `label` is optional and, when given
    for a multi-file upload, is suffixed per file (` (1)`, ` (2)`, ...) so the batch stays
    distinguishable in the vault without forcing per-file naming in the same form.
    """
    lang = resolve_interface_language()
    label = label.strip()
    multi = len(documents) > 1

    files: list[tuple[bytes, str]] = []
    for document in documents:
        content = document.file.read(_MAX_DOCUMENT_BYTES + 1)
        if len(content) > _MAX_DOCUMENT_BYTES:
            raise HTTPException(
                status_code=413, detail=_i18n.translate("additional_info.document_too_large", lang)
            )
        if not content:
            raise HTTPException(
                status_code=400, detail=_i18n.translate("additional_info.invalid_document", lang)
            )
        files.append((content, document.content_type or "application/octet-stream"))

    try:
        for index, (content, mime_type) in enumerate(files, start=1):
            file_label = f"{label} ({index})" if label and multi else (label or None)
            case_actions.attach_document(session, case_id, content, mime_type, label=file_label)
    except (CaseNotFoundError, InvalidCaseStateError) as exc:
        raise _not_found_or_wrong_state(exc) from exc
    except InvalidInputError as exc:
        raise HTTPException(
            status_code=400, detail=_i18n.translate("additional_info.invalid_document", lang)
        ) from exc
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)
