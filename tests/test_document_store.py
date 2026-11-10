"""Tests for the sensitive-document lifecycle (`rtbf_agent.document_store`, issue #17; document
vault, issue #111).

Verifies: nothing plaintext ever hits disk, attach requires an APPROVED decision and never destroys
the file (issue #111: a vault document survives being attached, reusable across sends/cases), and
TTL-expired *legacy* documents still get purged with a re-upload notice -- without ever touching
`Case.state` (R9). Explicit vault deletion (`delete_vault_document()`) is the only way a document
uploaded after #111 ever leaves the vault.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from rtbf_agent import document_store
from rtbf_agent.core.interfaces import (
    ApprovalDecision,
    NotificationMessage,
    Notifier,
)
from rtbf_agent.core.models import Case, CaseState, DocumentStatus, Event
from rtbf_agent.document_store import (
    decrypt_for_attach,
    decrypt_for_view,
    delete_vault_document,
    purge_expired_documents,
    record_document_attached,
    resolve_document_storage_dir,
    store_document,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(autouse=True)
def _document_env(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RTBF_DOCUMENT_ENCRYPTION_KEY", "zJ8gk8h1qYw3F1v3l4v0m9C7b3e9r0g5t9p9nQe8s0A="
    )
    monkeypatch.setenv("RTBF_DOCUMENT_STORAGE_DIR", str(tmp_path / "documents"))


class _FakeNotifier(Notifier):
    def __init__(self) -> None:
        self.notifications: list[NotificationMessage] = []

    async def notify(self, message: NotificationMessage) -> None:
        self.notifications.append(message)

    async def request_approval(self, request):  # noqa: ANN001
        raise NotImplementedError

    async def receive_document(self, case_id: str):  # noqa: ANN001
        raise NotImplementedError

    async def receive_text_response(self, case_id: str):  # noqa: ANN001
        raise NotImplementedError

    async def download_document_bytes(self, content_ref: str):  # noqa: ANN001
        raise NotImplementedError


def _make_case(session: Session) -> Case:
    case = Case(site="Example", domain="example.com", target_email="user@example.com")
    session.add(case)
    session.commit()
    session.refresh(case)
    return case


def test_uploaded_document_never_appears_plaintext_on_disk(session):
    case = _make_case(session)
    content = b"this is my passport scan"

    document = store_document(session, case, content, mime_type="image/jpeg")
    session.commit()

    storage_dir = resolve_document_storage_dir()
    on_disk_files = list(storage_dir.glob("*.enc"))
    assert len(on_disk_files) == 1
    raw_bytes = on_disk_files[0].read_bytes()
    assert content not in raw_bytes

    events = session.exec(select(Event).where(Event.case_id == case.id)).all()
    assert len(events) == 1
    assert events[0].kind == "document_received"
    assert "hash" in events[0].detail and "size" in events[0].detail
    assert document.status == DocumentStatus.AVAILABLE


def test_store_document_accepts_an_optional_label(session):
    case = _make_case(session)
    document = store_document(session, case, b"content", mime_type="image/png", label="ID card")
    session.commit()
    assert document.label == "ID card"


def test_attaching_a_document_never_deletes_the_file_or_changes_its_status(session):
    """Issue #111: a vault document survives being attached -- the old single-use
    `mark_attached_and_secure_delete()` is gone; `record_document_attached()` only records the
    audit event."""
    case = _make_case(session)
    content = b"identity document bytes"
    document = store_document(session, case, content, mime_type="application/pdf")
    session.commit()

    encrypted_path = document.encrypted_path
    plaintext = decrypt_for_attach(document, ApprovalDecision.APPROVED)
    assert plaintext == content
    assert Path(encrypted_path).exists()

    record_document_attached(session, document, case_id=case.id)
    session.commit()

    assert Path(encrypted_path).exists()  # still there -- not consumed
    session.refresh(document)
    assert document.status == DocumentStatus.AVAILABLE  # still available for reuse

    kinds = [
        event.kind for event in session.exec(select(Event).where(Event.case_id == case.id)).all()
    ]
    assert kinds == ["document_received", "document_attached"]


def test_a_vault_document_can_be_attached_to_two_different_cases(session):
    """The actual point of the vault: reuse across cases, not just across rounds of the same
    case."""
    case_a = _make_case(session)
    case_b = _make_case(session)
    document = store_document(session, case_a, b"my id card", mime_type="image/png")
    session.commit()

    plaintext_a = decrypt_for_attach(document, ApprovalDecision.APPROVED)
    record_document_attached(session, document, case_id=case_a.id)
    plaintext_b = decrypt_for_attach(document, ApprovalDecision.APPROVED)
    record_document_attached(session, document, case_id=case_b.id)
    session.commit()

    assert plaintext_a == plaintext_b == b"my id card"
    session.refresh(document)
    assert document.status == DocumentStatus.AVAILABLE

    attached_events = session.exec(select(Event).where(Event.kind == "document_attached")).all()
    assert sorted(event.case_id for event in attached_events) == sorted([case_a.id, case_b.id])


def test_attach_rejects_a_non_approved_decision(session):
    case = _make_case(session)
    document = store_document(session, case, b"content", mime_type="image/png")
    session.commit()

    with pytest.raises(ValueError, match="APPROVED"):
        decrypt_for_attach(document, ApprovalDecision.REJECTED)


def test_attach_rejects_a_document_that_is_not_available(session):
    case = _make_case(session)
    document = store_document(session, case, b"content", mime_type="image/png")
    session.commit()
    delete_vault_document(session, document)
    session.commit()

    with pytest.raises(ValueError, match="not available"):
        decrypt_for_attach(document, ApprovalDecision.APPROVED)


def test_decrypt_for_view_returns_the_original_plaintext(session):
    case = _make_case(session)
    document = store_document(session, case, b"my id card", mime_type="image/png")
    session.commit()

    assert decrypt_for_view(document) == b"my id card"


def test_decrypt_for_view_refuses_a_purged_document(session):
    case = _make_case(session)
    document = store_document(session, case, b"content", mime_type="image/png")
    session.commit()
    delete_vault_document(session, document)
    session.commit()

    with pytest.raises(ValueError, match="not available"):
        decrypt_for_view(document)


def test_decrypt_for_view_takes_no_approval_decision():
    """Signature-level, not just behavioral, proof that `decrypt_for_view()` cannot be used as a
    bypass of `decrypt_for_attach()`'s R1 approval gate -- there is no `ApprovalDecision` parameter
    to pass one to at all."""
    import inspect

    parameters = inspect.signature(decrypt_for_view).parameters
    assert "decision" not in parameters
    assert not any(
        parameter.annotation == ApprovalDecision or parameter.annotation == "ApprovalDecision"
        for parameter in parameters.values()
    )


def test_delete_vault_document_secure_deletes_and_marks_purged(session):
    case = _make_case(session)
    document = store_document(session, case, b"content", mime_type="image/png")
    session.commit()
    encrypted_path = document.encrypted_path

    delete_vault_document(session, document)
    session.commit()

    assert not Path(encrypted_path).exists()
    session.refresh(document)
    assert document.status == DocumentStatus.PURGED

    kinds = [
        event.kind for event in session.exec(select(Event).where(Event.case_id == case.id)).all()
    ]
    assert kinds == ["document_received", "document_purged"]


def test_new_vault_uploads_have_no_ttl(session):
    """Issue #111: no automatic expiry for a document uploaded going forward."""
    case = _make_case(session)
    document = store_document(session, case, b"content", mime_type="image/png")
    session.commit()
    assert document.ttl_expires_at is None


async def test_purge_expired_documents_only_matches_documents_with_a_ttl_set(session):
    """A new vault upload (no `ttl_expires_at`) must never be purged, no matter how old --
    `purge_expired_documents()` only ever matches a legacy pre-#111 document that still has one."""
    case = _make_case(session)
    document = store_document(session, case, b"content", mime_type="image/png")
    document.received_at = datetime.now(UTC) - timedelta(days=365)
    session.commit()

    purged = await purge_expired_documents(session, _FakeNotifier())
    session.commit()

    assert purged == []
    session.refresh(document)
    assert document.status == DocumentStatus.AVAILABLE


async def test_document_purged_after_ttl_expiry(session):
    """A legacy document (one that still has a `ttl_expires_at` set) is still purged once
    expired."""
    case = _make_case(session)
    document = store_document(session, case, b"expired content", mime_type="image/png")
    document.ttl_expires_at = datetime.now(UTC) - timedelta(hours=1)
    session.commit()

    notifier = _FakeNotifier()
    purged = await purge_expired_documents(session, notifier)
    session.commit()

    assert len(purged) == 1
    session.refresh(document)
    assert document.status == DocumentStatus.PURGED
    assert not Path(document.encrypted_path).exists()

    kinds = [
        event.kind for event in session.exec(select(Event).where(Event.case_id == case.id)).all()
    ]
    assert kinds == ["document_received", "document_purged"]


async def test_purge_continues_past_one_documents_delete_failure(session, monkeypatch):
    case = _make_case(session)
    broken = store_document(session, case, b"missing file", mime_type="image/png")
    broken.ttl_expires_at = datetime.now(UTC) - timedelta(hours=1)
    ok = store_document(session, case, b"deletable file", mime_type="image/png")
    ok.ttl_expires_at = datetime.now(UTC) - timedelta(hours=1)
    session.commit()

    # Force the first delete to fail regardless of OS permission semantics (a plain `chmod(0o000)`
    # doesn't simulate a failure when tests run as root, since root bypasses DAC checks).
    real_secure_delete = document_store._secure_delete_file

    def _flaky_secure_delete(path: Path) -> None:
        if str(path) == broken.encrypted_path:
            raise OSError("simulated delete failure")
        real_secure_delete(path)

    monkeypatch.setattr(document_store, "_secure_delete_file", _flaky_secure_delete)

    notifier = _FakeNotifier()
    purged = await purge_expired_documents(session, notifier)
    session.commit()

    assert [document.id for document in purged] == [ok.id]
    session.refresh(broken)
    session.refresh(ok)
    assert broken.status == DocumentStatus.AVAILABLE
    assert ok.status == DocumentStatus.PURGED


async def test_purge_triggers_reupload_notification(session):
    case = _make_case(session)
    document = store_document(session, case, b"expired content", mime_type="image/png")
    document.ttl_expires_at = datetime.now(UTC) - timedelta(hours=1)
    session.commit()

    notifier = _FakeNotifier()
    await purge_expired_documents(session, notifier)

    assert len(notifier.notifications) == 1
    assert notifier.notifications[0].case_id == str(case.id)
    assert "re-upload" in notifier.notifications[0].text.lower()


async def test_purge_never_touches_case_state(session):
    case = _make_case(session)
    case.state = CaseState.AWAITING_RESPONSE
    document = store_document(session, case, b"expired content", mime_type="image/png")
    document.ttl_expires_at = datetime.now(UTC) - timedelta(hours=1)
    session.commit()

    await purge_expired_documents(session, _FakeNotifier())
    session.commit()

    session.refresh(case)
    assert case.state == CaseState.AWAITING_RESPONSE
