"""Encrypted-at-rest storage and lifecycle for sensitive identity documents (spec §3.4, issue #17).

Minimal first pass: one env var for the encryption key, one for the storage directory, one TTL
constant. Nothing here talks to Telegram directly -- callers hand this module plaintext bytes
(already retrieved by a `Notifier`) and get a `Document` handle back; `Document.encrypted_path`
never holds plaintext (R6).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet
from sqlmodel import Session, col, select

from rtbf_agent.core.interfaces import ApprovalDecision, NotificationMessage, Notifier
from rtbf_agent.core.models import Case, Document, DocumentStatus, create_event

DEFAULT_STORAGE_DIR = Path.home() / ".local" / "share" / "rtbf-agent" / "documents"

_OVERWRITE_CHUNK_SIZE = 64 * 1024

_logger = logging.getLogger(__name__)


def resolve_document_encryption_key() -> bytes:
    raw = os.environ.get("RTBF_DOCUMENT_ENCRYPTION_KEY")
    if not raw:
        raise ValueError(
            "RTBF_DOCUMENT_ENCRYPTION_KEY is not set -- generate one with "
            '`python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"` '
            "and set it as an env var before storing any document"
        )
    return raw.encode()


def resolve_document_storage_dir() -> Path:
    raw = os.environ.get("RTBF_DOCUMENT_STORAGE_DIR")
    storage_dir = Path(raw).expanduser() if raw else DEFAULT_STORAGE_DIR
    storage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return storage_dir


def store_document(
    session: Session, case: Case, content: bytes, mime_type: str, *, label: str | None = None
) -> Document:
    """Encrypt `content` and persist it into the vault, returning a `Document` handle (never the
    plaintext) -- issue #111: no TTL, no single-use consumption; the document stays available
    (`DocumentStatus.AVAILABLE`) until a human explicitly deletes it
    (`delete_vault_document()`)."""
    if case.id is None:
        raise RuntimeError("case must be persisted (have an id) before it can hold a document")
    key = resolve_document_encryption_key()
    ciphertext = Fernet(key).encrypt(content)

    storage_dir = resolve_document_storage_dir()
    encrypted_path = storage_dir / f"{uuid.uuid4().hex}.enc"
    # `os.open` with an explicit mode, not `Path.write_bytes()`: the latter respects the process
    # umask and can leave the ciphertext file group/other-readable, leaking its existence/size even
    # though the content itself is encrypted.
    fd = os.open(encrypted_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(ciphertext)

    document = Document(
        case_id=case.id,
        status=DocumentStatus.AVAILABLE,
        label=label,
        encrypted_path=str(encrypted_path),
        mime_type=mime_type,
        size=len(content),
        sha256_hash=hashlib.sha256(content).hexdigest(),
    )
    session.add(document)
    try:
        session.flush()
    except Exception:
        # The ciphertext file was already written above; if the `Document` row itself fails to
        # persist (e.g. a `case_id` FK violation), don't leave an orphaned encrypted file with no
        # DB record pointing at it -- no plaintext leak either way, but it'd otherwise sit on disk
        # forever with nothing to ever purge it.
        encrypted_path.unlink(missing_ok=True)
        raise

    session.add(
        create_event(
            case_id=case.id,
            kind="document_received",
            actor="system",
            detail={"hash": document.sha256_hash, "size": document.size},
        )
    )
    return document


def _secure_delete_file(path: Path) -> None:
    """Overwrite the file's bytes before unlinking it, rather than a plain unlink (R7).

    Not a guarantee against recovery on every filesystem (SSD wear-leveling, copy-on-write
    filesystems, and journaling can all retain a copy of the old data elsewhere on disk) -- best
    effort, matching the honesty this project already applies to platform-boundary limitations
    (R10/R11). Overwrites in fixed-size chunks rather than one `os.urandom(size)` call so memory
    use stays bounded regardless of document size. Raises `OSError` on failure -- callers that need
    "keep going even if one file fails" (the purge sweep) catch it themselves; a single-document
    attach should still surface the failure rather than silently pretend it succeeded.
    """
    if not path.exists():
        return
    size = path.stat().st_size
    with path.open("r+b") as fh:
        remaining = size
        while remaining > 0:
            chunk = min(_OVERWRITE_CHUNK_SIZE, remaining)
            fh.write(os.urandom(chunk))
            remaining -= chunk
        fh.flush()
        os.fsync(fh.fileno())
    path.unlink()


def decrypt_for_attach(document: Document, decision: ApprovalDecision) -> bytes:
    """Decrypt `document`'s plaintext for the caller to attach to the outgoing message (R1).

    Requires the caller to pass the already-obtained `ApprovalDecision` -- raises rather than
    decrypting if it isn't `APPROVED`, so this can't be called as a bypass of the approval gate
    the rest of the codebase enforces before any irreversible action. Never mutates `document` or
    modifies the file on disk (it only reads the ciphertext to decrypt it) -- issue #111: a vault
    document survives being attached (it's meant to be reused), so there's nothing here to defer
    until "after the send succeeds" the way the old single-use flow had to.
    """
    if decision is not ApprovalDecision.APPROVED:
        raise ValueError("cannot attach a document without an APPROVED decision")
    if document.status is not DocumentStatus.AVAILABLE:
        raise ValueError(f"document {document.id} is not available (is {document.status})")

    key = resolve_document_encryption_key()
    path = Path(document.encrypted_path)
    return Fernet(key).decrypt(path.read_bytes())


def decrypt_for_view(document: Document) -> bytes:
    """Decrypt `document`'s plaintext for the human's own local inspection -- e.g. downloading it
    from the vault page to confirm "is `document-4` actually my ID card or my proof of address?".

    Deliberately takes **no `ApprovalDecision` and enforces no approval gate** -- unlike
    `decrypt_for_attach()` above, this never hands the plaintext to anything that could transmit
    it to a third party. It only ever flows back to the same human who uploaded it in the first
    place, inspecting their own previously-stored file; there is no "send" on the other end of this
    call for R1 to gate. The actual send path is untouched and still only ever goes through
    `decrypt_for_attach()`, which still requires `ApprovalDecision.APPROVED`. Still refuses a
    purged document (`document.status is not AVAILABLE`), same as `decrypt_for_attach()`, since
    there's nothing left on disk to decrypt once a document has been secure-deleted.
    """
    if document.status is not DocumentStatus.AVAILABLE:
        raise ValueError(f"document {document.id} is not available (is {document.status})")

    key = resolve_document_encryption_key()
    path = Path(document.encrypted_path)
    return Fernet(key).decrypt(path.read_bytes())


def record_document_attached(session: Session, document: Document, *, case_id: int) -> None:
    """Record that `document` was attached to a send for `case_id` -- call after a successful send.

    Plain, synchronous, no file I/O (issue #111): unlike the old `mark_attached_and_secure_delete`,
    a vault document is never destroyed or status-changed by being used -- reuse across many sends,
    and across cases other than the one it was originally uploaded on, is the whole point. `case_id`
    is the *target* case (the one actually being sent for), which may differ from
    `document.case_id` (the case it happened to be uploaded against) -- this mirrors how every other
    `Event` in this codebase is recorded with whatever case is relevant at record time, independent
    of any other row's own `case_id`.
    """
    session.add(
        create_event(
            case_id=case_id,
            kind="document_attached",
            actor="human",
            detail={"hash": document.sha256_hash, "size": document.size},
        )
    )


def _mark_document_purged(session: Session, document: Document, *, actor: str) -> None:
    """Flip `document.status` to `PURGED` and record the `document_purged` `Event` -- the part of
    "purge a document" shared by `delete_vault_document()` (human, sync) and
    `purge_expired_documents()`'s per-document loop (system, async) below, factored out (OCR
    review finding) so a future change to the event-detail shape or status transition can't drift
    between the two callers. Deliberately does *not* secure-delete the file itself: each caller's
    file-deletion strategy differs (sync vs. `asyncio.to_thread`, and `purge_expired_documents()`
    must skip this call entirely on a delete failure -- see its own docstring), so only the part
    that's genuinely identical either way lives here.
    """
    document.status = DocumentStatus.PURGED
    session.add(document)
    session.add(
        create_event(
            case_id=document.case_id,
            kind="document_purged",
            actor=actor,
            detail={"hash": document.sha256_hash, "size": document.size},
        )
    )


def delete_vault_document(session: Session, document: Document) -> None:
    """Secure-delete `document`'s file and mark it `PURGED` (R7) -- the manual counterpart of what
    TTL-purge used to do automatically for every document (issue #111: vault documents have no TTL,
    so this explicit action is now the only way a document ever leaves the vault, besides the
    legacy TTL sweep in `purge_expired_documents()` below)."""
    _secure_delete_file(Path(document.encrypted_path))
    _mark_document_purged(session, document, actor="human")


async def purge_expired_documents(session: Session, notifier: Notifier) -> list[Document]:
    """Secure-delete and mark `PURGED` every document whose TTL has expired (R8).

    Issue #111: a vault document uploaded going forward has `ttl_expires_at=None` (no automatic
    expiry -- kept until a human explicitly calls `delete_vault_document()`), so this only ever
    matches a document uploaded *before* #111 shipped, which still has a real TTL set. Kept, not
    removed, purely for that one-time legacy transition -- there is no code path left that sets a
    new `ttl_expires_at` going forward, so this function's match set can only ever shrink to empty
    over time, never grow.

    Never touches `Case.state` (R9) -- purging a document is independent of the case it belongs
    to. Notifies the human so they know to re-upload if the document is still needed.

    Known limitations, both deferred because nothing calls this yet (issue #17 is storage/
    lifecycle primitives only -- these are decisions for whichever scheduler wires this in, issue
    #69):
    - No concurrency protection (no `SELECT ... FOR UPDATE`/row locking) against two overlapping
      calls both fetching and processing the same expired documents.
    - **Accepted data-loss risk, not yet mitigated**: this function doesn't commit the session
      itself (matching `store_document`/`record_document_attached`'s "caller controls the
      transaction" convention). The file is deleted *before* `document.status` is flipped to
      `PURGED` (deliberately -- a delete failure then leaves the document `AVAILABLE` and
      eligible for retry, rather than stuck `PURGED` forever with an undeleted file), but that
      means if the *caller's* subsequent `session.commit()` fails or the process crashes right
      after this returns, the file is already irrecoverably gone while the DB still shows
      `AVAILABLE` -- unlike a failed `Sender.send()` retry (idempotent: sending again is
      safe), there is no way to recover from this specific failure window short of asking the human
      to re-upload. Not fixed here because the right answer (commit-per-document inside this
      function? a different transaction boundary entirely?) is a call for whichever caller/
      scheduler issue #69 builds, not a call this storage-primitives-only module should make
      unilaterally.
    """
    now = datetime.now(UTC)
    expired = session.exec(
        select(Document).where(
            Document.status == DocumentStatus.AVAILABLE,
            col(Document.ttl_expires_at).is_not(None),
            col(Document.ttl_expires_at) <= now,
        )
    ).all()

    purged: list[Document] = []
    for document in expired:
        try:
            # `_secure_delete_file` is blocking file I/O (fsync included); run it off the event
            # loop so one large document's overwrite doesn't stall every other coroutine.
            await asyncio.to_thread(_secure_delete_file, Path(document.encrypted_path))
        except OSError:
            _logger.exception(
                "failed to secure-delete document %s; leaving it for the next purge pass",
                document.id,
            )
            continue
        _mark_document_purged(session, document, actor="system")

        await notifier.notify(
            NotificationMessage(
                case_id=str(document.case_id),
                text=(
                    "Your uploaded document expired before being approved and was securely "
                    "deleted. Please re-upload it if it's still needed."
                ),
            )
        )
        purged.append(document)

    return purged
