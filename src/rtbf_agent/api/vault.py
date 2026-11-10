"""The document vault (issue #111): browse/delete every document currently in the vault, and
select existing vault document(s) for a case's current `Requires human action` round without
re-uploading anything -- the web-actionable counterpart of the CLI's
`list-documents`/`select-documents`/`delete-document` subcommands.

**The actual decision-recording logic lives in `case_actions.py`**
(`list_vault_documents()`/`select_documents_for_resend()`/`delete_vault_document()`), shared with
those CLI subcommands -- this file is just the HTTP adapter: parse, call, translate
`case_actions`' plain exceptions to the matching `HTTPException` + i18n string, mirroring
`document.py`/`approval.py`/`recipient.py`'s established pattern exactly.

**Records only, never resends here** -- see `case_actions.select_documents_for_resend()`'s own
docstring for why the actual draft/resend is a separate, later, explicit human action
(`followup.draft_additional_info_response()`, then `case_actions.approve_draft()`), never this
route's.

**R23:** every user-facing string this module returns is sourced from `I18nProvider.translate()`,
same invariant every other `api/` module already enforces, checked the same mechanical way (see
`tests/api/test_vault.py`).
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from rtbf_agent import case_actions, document_store
from rtbf_agent.api.deps import get_session
from rtbf_agent.case_actions import (
    CaseActionError,
    CaseNotFoundError,
    InvalidCaseStateError,
    InvalidInputError,
)
from rtbf_agent.config import resolve_interface_language
from rtbf_agent.core.interfaces import I18nProvider
from rtbf_agent.core.models import Document, DocumentStatus
from rtbf_agent.plugins.i18n.resource import ResourceI18nProvider

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# One `I18nProvider` instance for the whole process, mirroring `document.py`/`approval.py`.
_i18n: I18nProvider = ResourceI18nProvider()


@router.get("/vault")
def vault_page(request: Request, session: Session = Depends(get_session)):
    """List every document currently in the vault (`DocumentStatus.AVAILABLE`), most recent
    first, each with a delete button -- purely a browse/manage view, no case context here (a
    document is selected for a specific case's resend from that case's own detail page instead,
    see `select_documents_for_resend()` below)."""
    lang = resolve_interface_language()
    documents = case_actions.list_vault_documents(session)
    context = {
        "lang": lang,
        "page_title": _i18n.translate("vault.page_title", lang),
        "back_link_text": _i18n.translate("vault.back_link_text", lang),
        "empty_text": _i18n.translate("vault.empty", lang),
        "delete_button_text": _i18n.translate("vault.delete_button", lang),
        "download_link_text": _i18n.translate("vault.download_link_text", lang),
        "rename_label_text": _i18n.translate("vault.rename_label", lang),
        "rename_button_text": _i18n.translate("vault.rename_button", lang),
        "size_unit_text": _i18n.translate("vault.size_unit", lang),
        "metadata_separator_text": _i18n.translate("vault.metadata_separator", lang),
        "documents": [
            {
                "id": document.id,
                "label": document.label or f"document-{document.id}",
                "mime_type": document.mime_type,
                "size": document.size,
                "received_at": document.received_at.isoformat(),
                "upload_hint": _i18n.translate("vault.upload_hint", lang, case_id=document.case_id),
            }
            for document in documents
        ],
    }
    return templates.TemplateResponse(request, "vault.html", context)


@router.get("/vault/{document_id:int}/download")
def download_document(document_id: int, session: Session = Depends(get_session)) -> Response:
    """Decrypt and return `document_id`'s plaintext bytes for the human's own local inspection --
    e.g. confirming an unlabeled `document-4` is actually the ID card and not the proof of address
    before selecting it for a round.

    Deliberately goes through `document_store.decrypt_for_view()`, not `decrypt_for_attach()` --
    see that function's own docstring for why no `ApprovalDecision`/R1 gate applies here: this
    bytes-out response only ever reaches the same human who uploaded the file, never a third party
    (the actual send path is untouched and still only goes through `decrypt_for_attach()`).

    404s (the same `vault.not_found` i18n string `delete_document()` already uses) for a missing or
    already-purged document, rather than distinguishing "never existed" from "was deleted" -- same
    information a human needs either way ("that document isn't there"), and mirrors every other
    vault route's not-found handling.
    """
    lang = resolve_interface_language()
    document = session.get(Document, document_id)
    if document is None or document.status is not DocumentStatus.AVAILABLE:
        raise HTTPException(status_code=404, detail=_i18n.translate("vault.not_found", lang))
    content = document_store.decrypt_for_view(document)

    label = document.label or f"document-{document.id}"
    # Strip characters that could break out of the quoted `filename="..."` header value or inject a
    # second header via a stray CR/LF -- the label is human-supplied free text (rename_document()
    # below accepts anything non-blank), not something this route can otherwise trust verbatim.
    # Backslash included (OCR review finding): per RFC 6266 SS4.1, `\` is the quoted-string escape
    # character, so a label ending in `\` (e.g. "foo\") would turn the closing `"` into an escaped
    # literal quote (`\"`) instead of terminating the string, leaving the header malformed.
    safe_label = "".join(ch for ch in label if ch not in '"\r\n\\')
    extension = mimetypes.guess_extension(document.mime_type) or ""
    filename = (
        safe_label
        if safe_label.endswith(extension) or not extension
        else f"{safe_label}{extension}"
    )
    return Response(
        content=content,
        media_type=document.mime_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/vault/{document_id:int}/rename")
def rename_document(
    document_id: int, label: str = Form(...), session: Session = Depends(get_session)
) -> RedirectResponse:
    """Rename an existing vault document (e.g. giving a currently-unlabeled `document-4` a real
    name) -- always allowed, not scoped to any case's round, same shape as `delete_document()`
    above (`case_actions.rename_vault_document()`'s own docstring)."""
    lang = resolve_interface_language()
    try:
        case_actions.rename_vault_document(session, document_id, label)
    except InvalidInputError as exc:
        # `case_actions.rename_vault_document()` raises the same `InvalidInputError` for two
        # distinct causes -- "document doesn't exist/isn't in the vault" (404, same as every other
        # vault route's not-found handling) and "label was blank" (400) -- so re-check which one
        # actually happened rather than collapsing both into one status code.
        document = session.get(Document, document_id)
        if document is None or document.status is not DocumentStatus.AVAILABLE:
            raise HTTPException(
                status_code=404, detail=_i18n.translate("vault.not_found", lang)
            ) from exc
        raise HTTPException(
            status_code=400, detail=_i18n.translate("vault.invalid_rename", lang)
        ) from exc
    return RedirectResponse(url="/vault", status_code=303)


@router.post("/vault/{document_id:int}/delete")
def delete_document(document_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    """Permanently remove a document from the vault (secure-delete) -- always allowed, not scoped
    to any case's round (`case_actions.delete_vault_document()`'s own docstring)."""
    lang = resolve_interface_language()
    try:
        case_actions.delete_vault_document(session, document_id)
    except InvalidInputError as exc:
        raise HTTPException(
            status_code=404, detail=_i18n.translate("vault.not_found", lang)
        ) from exc
    return RedirectResponse(url="/vault", status_code=303)


@router.post("/cases/{case_id:int}/select-documents")
def select_documents(
    case_id: int,
    document_ids: list[int] | None = Form(default=None),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Select one or more existing vault documents for `case_id`'s current
    `Requires human action` round, without re-uploading anything.

    Plain `def`, not `async def` (matches `document.py`'s routes): the handler does nothing but a
    synchronous `case_actions` call.

    `document_ids` defaults to an empty list, not `Form(...)` (required): a browser omits the field
    entirely from the POST body when no checkbox was checked, which against a required `Form(...)`
    FastAPI rejects with a generic 422 before this handler ever runs -- defaulting to `[]` instead
    lets `case_actions.select_documents_for_resend()`'s own "must select at least one document"
    check produce the same i18n-resolved error message the upload form's empty-content case
    already gets.
    """
    lang = resolve_interface_language()
    try:
        case_actions.select_documents_for_resend(session, case_id, document_ids or [])
    except CaseNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=_i18n.translate("vault.case_not_found", lang)
        ) from exc
    except InvalidCaseStateError as exc:
        raise HTTPException(
            status_code=409,
            detail=_i18n.translate("additional_info.wrong_state", lang, case_id=exc.case_id),
        ) from exc
    except CaseActionError as exc:
        raise HTTPException(
            status_code=400, detail=_i18n.translate("vault.invalid_selection", lang)
        ) from exc
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)
