"""Core data model: `Case` and `Event` SQLModel tables (spec §3.2).

Two invariants this module exists to hold, both cited in
`docs/planning/RESTRICTIONS.md`:

- **`Event` is append-only (R4).** No update or delete method exists for `Event` anywhere in this
  module -- the only supported way to persist one is `create_event()` followed by a plain insert
  (`session.add(event)`). This is also enforced at the SQLAlchemy level: a `before_update` mapper
  listener on `Event` raises `RuntimeError` if any code path (this module, a future repository
  layer, or a raw `session.add()` of a mutated row) ever attempts an `UPDATE` against an existing
  `Event` row -- it is not merely a naming convention that a linter-style test can miss.
- **Document-related `Event.detail` never carries content (R5).** For the four document-related
  event kinds (`DOCUMENT_EVENT_KINDS`), `detail` may only hold `{hash, size, extension?}`.
  `create_event()` enforces this at construction time via `validate_document_event_detail()`, and
  `Event` itself additionally enforces it via a SQLAlchemy `@validates` hook on `kind`/`detail` --
  so the check fires even if someone constructs `Event(...)` directly instead of going through
  `create_event()`, and again on any later attempt to reassign `kind` or `detail` on an in-memory
  instance before it's flushed.

A third, easy-to-miss nuance from the spec (§3.3, R9/R22): `Case.applicable_jurisdiction`,
`Case.drafting_language`, `Case.reminder_enabled`, and `Case.reminder_interval_days` are all
nullable **per-case overrides** of a global default that lives outside this module (in user
config). `None` means "defer to the global default"; a non-`None` value on the case always wins,
regardless of what the global default is. None of these four fields is ever derived from another
field on `Case`, from `interface_language`, or from one another -- each is independently settable.
This module does not know what the global defaults are; resolving a case's effective value against
them is the caller's job (see `resolved_*` helpers on `Case`, which take the global default as an
explicit argument rather than reading it from anywhere).
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column, DateTime
from sqlalchemy import event as sa_event
from sqlalchemy.orm import validates
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """A `DateTime` column that round-trips as UTC-aware regardless of the DB backend.

    `_utcnow()` returns timezone-aware datetimes, but plain `DateTime(timezone=True)` only
    preserves that through PostgreSQL and similar backends -- SQLite (this project's default, per
    `CLAUDE.md`) has no native timezone-aware datetime type, so `timezone=True` there is a no-op
    and tzinfo is silently dropped on write. This decorator normalizes to naive UTC on write and
    reattaches `tzinfo=UTC` on read, so callers always get a UTC-aware datetime back no matter
    which backend is configured.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime not allowed here; all datetimes must be UTC-aware")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class CaseState(enum.StrEnum):
    """The 9 kanban columns a `Case` moves through, in order (spec §3.2, simplified during the
    automated-case-loop rework).

    `Discovered -> Classified -> Draft ready -> Awaiting human approval -> Awaiting response ->
    Requires human action -> Deletion confirmed -> Rejected/Escalated -> Closed`. There is
    deliberately no separate `Sent` state: a successful send moves a case straight to
    `Awaiting response` (a case is sent and immediately awaiting a reply -- there is no meaningful
    gap between the two; `_execute_send()` is the only place this transition happens, and
    `request_sent`'s own `Event` is still the durable, permanent record that a send genuinely
    happened, same as always). There is also deliberately no "timed out" or "auto-escalated" state
    and no field on `Case` tracks a deadline: R9 requires that waiting states have no timeout,
    auto-close, or auto-escalation, only an opt-in reminder *notice* (handled entirely outside this
    model, see `Case.reminder_enabled`/`reminder_interval_days`).
    """

    DISCOVERED = "Discovered"
    CLASSIFIED = "Classified"
    DRAFT_READY = "Draft ready"
    AWAITING_HUMAN_APPROVAL = "Awaiting human approval"
    AWAITING_RESPONSE = "Awaiting response"
    REQUIRES_HUMAN_ACTION = "Requires human action"
    DELETION_CONFIRMED = "Deletion confirmed"
    REJECTED_ESCALATED = "Rejected/Escalated"
    CLOSED = "Closed"


# The 9 states in kanban column order, exposed so callers (tests included) don't have to
# re-derive or hardcode the ordering themselves.
CASE_STATES_IN_ORDER: tuple[CaseState, ...] = tuple(CaseState)


class Case(SQLModel, table=True):
    """A detected site/account under an active or closed RTBF case (spec §3.2)."""

    id: int | None = Field(default=None, primary_key=True)
    site: str
    domain: str
    target_email: str
    state: CaseState = Field(default=CaseState.DISCOVERED)

    # The site/company's real deletion-request recipient (its privacy/DPO contact) -- where an
    # actual deletion request is emailed. Deliberately distinct from `target_email`, which is the
    # user's *own* scanned mailbox (the address discovery attributes this account to); sending the
    # request there would email the user themselves, never the company (issue #52). Nullable
    # because discovery cannot determine it (R20: no hardcoded site logic in core/ -- and inferring
    # a privacy contact from the domain is explicitly out of scope here, tracked separately as
    # issue #64): it is set manually, human-confirmed, before a send is ever solicited. The send
    # path (`agent_loop._execute_send`/`send_request`) refuses to send while this is unset rather
    # than ever falling back to `target_email` -- a human approving a send (R1) must be approving a
    # send to a specific, real, confirmed recipient, not silently to the user's own inbox.
    recipient_email: str | None = Field(default=None)

    # Per-case overrides of global defaults (config lives outside core/) -- nullable, resolved via
    # `resolved_applicable_jurisdiction`/`resolved_drafting_language` below. Never derived from
    # each other or from interface language (R22): a case's jurisdiction and drafting language are
    # two independent axes, and both are independent of interface language, which has no per-case
    # field at all.
    applicable_jurisdiction: str | None = Field(default=None)
    drafting_language: str | None = Field(default=None)

    # UTCDateTime, not plain DateTime(timezone=True): see the class docstring above -- SQLite
    # (this project's default) silently drops tzinfo through timezone=True alone. nullable=False
    # matches the non-Optional `datetime` type annotation -- without it, `case.x = None` would be
    # silently persisted, then raise a confusing AttributeError on `.tzinfo` far from the cause.
    discovered_at: datetime = Field(
        default_factory=_utcnow, sa_column=Column(UTCDateTime, nullable=False)
    )
    discovery_evidence: str | None = Field(default=None)
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(UTCDateTime, nullable=False, onupdate=_utcnow),
    )

    # Per-case reminder override (R9; spec §3.3: "opcional y configurable por caso o
    # globalmente"). `None` means "defer to the global default" for both fields independently --
    # a case can, for instance, opt out of reminders (`False`) even while the global default is on,
    # or pick its own `reminder_interval_days` while leaving `reminder_enabled` deferred.
    reminder_enabled: bool | None = Field(default=None)
    reminder_interval_days: int | None = Field(default=None)

    def resolved_reminder_enabled(self, global_default: bool) -> bool:
        """Whether reminders are on for this case: its own setting, else the global default."""
        if self.reminder_enabled is None:
            return global_default
        return self.reminder_enabled

    def resolved_reminder_interval_days(self, global_default: int | None) -> int | None:
        """Reminder interval for this case: the case's own value, else the global default."""
        if self.reminder_interval_days is None:
            return global_default
        return self.reminder_interval_days

    def resolved_applicable_jurisdiction(self, global_default: str | None) -> str | None:
        """Applicable jurisdiction for this case: its own value, else the global default."""
        if self.applicable_jurisdiction is None:
            return global_default
        return self.applicable_jurisdiction

    def resolved_drafting_language(self, global_default: str | None) -> str | None:
        """Drafting/communication language for this case: its own value, else the global default."""
        if self.drafting_language is None:
            return global_default
        return self.drafting_language


class DocumentStatus(enum.StrEnum):
    """Lifecycle state of one uploaded sensitive document (spec §3.4, issue #17; vault, issue #111).

    Two states only: `AVAILABLE` (in the vault, usable -- may be attached to any number of sends
    across any number of cases) and `PURGED` (deleted, file gone). There is deliberately no
    "attached"/terminal-after-first-use status any more (issue #111, a documented, deliberate
    relaxation of the old "short TTL, secure delete after use" policy -- see `CLAUDE.md`): a vault
    document can be attached many times, so "was this ever used" is not a status flag at all, it's
    whatever `document_attached` `Event`s exist for it -- the same append-only audit trail every
    other usage fact in this codebase already lives in, never a mutable field on the row itself.
    """

    AVAILABLE = "Available"
    PURGED = "Purged"


class Document(SQLModel, table=True):
    """Handle for one uploaded sensitive document's encrypted-at-rest lifecycle (issue #17; vault,
    issue #111).

    Never holds the document's plaintext bytes -- only `encrypted_path` (where the Fernet
    ciphertext lives on disk) and metadata. `document_store.py` is the only module that reads or
    writes the file at `encrypted_path`.

    `case_id` records which case this document was originally uploaded *against* (and is what its
    `document_received` `Event` is recorded on) -- it is not a scope restricting which case may
    later *select* this document for a resend. A vault document can be attached to a different
    case's send than the one it was uploaded on; that reuse is exactly issue #111's point, and needs
    no schema change here because `document_attached`/`document_purged` `Event`s are always
    recorded with whatever case id is relevant at that moment (the target case, not `self.case_id`),
    the same way every other `Event` in this codebase is recorded independent of any other row's own
    `case_id`.
    """

    id: int | None = Field(default=None, primary_key=True)
    case_id: int = Field(foreign_key="case.id")
    status: DocumentStatus = Field(default=DocumentStatus.AVAILABLE)
    label: str | None = Field(default=None)
    """A human-friendly name (e.g. "ID card", "proof of address") -- `None` (the default) is fine
    while a human only ever has one document to choose from, but becomes actively confusing once
    the vault holds several; `_execute_send()` falls back to `document-{id}` as the attachment
    filename when unset, matching the pre-#111 behavior exactly."""
    encrypted_path: str
    mime_type: str
    size: int
    sha256_hash: str
    received_at: datetime = Field(
        default_factory=_utcnow, sa_column=Column(UTCDateTime, nullable=False)
    )
    ttl_expires_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime))
    """`None` (the default, issue #111) for every vault document going forward -- no automatic
    expiry, kept until a human explicitly deletes it (`document_store.delete_vault_document()`).
    Only ever non-`None` for a document uploaded before issue #111 shipped, which
    `purge_expired_documents()`'s TTL sweep still honors -- see that function's own docstring."""


# Event kinds whose `detail` refers to a sensitive document (e.g. an identification image).
# `create_event()` restricts `detail` for these kinds to `{hash, size, extension?}` -- never raw
# bytes or extracted text (R5).
DOCUMENT_EVENT_KINDS = frozenset(
    {
        "document_received",
        "document_attached",
        "document_sent",
        "document_purged",
    }
)

_DOCUMENT_DETAIL_ALLOWED_KEYS = frozenset({"hash", "size", "extension"})
_DOCUMENT_DETAIL_REQUIRED_KEYS = frozenset({"hash", "size"})


def validate_document_event_detail(detail: dict[str, Any]) -> None:
    """Raise `ValueError` if `detail` carries anything beyond a document's hash/size/extension.

    Enforces R5: a document-related `Event.detail` may only ever contain `hash` (required), `size`
    (required), and optionally `extension` -- never the document's raw bytes, a base64 encoding of
    it, or any extracted text. This does not merely warn against extra keys named "content" or
    similar -- it is an allowlist, so any key it doesn't recognize is rejected, including future
    ones nobody has thought to name yet. Value *types* are checked too, not just key names --
    otherwise `{"hash": 123, "size": "ten"}` would pass, undermining the guarantee for any
    downstream code that assumes `hash` is a digest string and `size` a byte count.
    """
    extra_keys = set(detail) - _DOCUMENT_DETAIL_ALLOWED_KEYS
    if extra_keys:
        raise ValueError(
            "document event detail may only contain "
            f"{sorted(_DOCUMENT_DETAIL_ALLOWED_KEYS)}, got extra keys: {sorted(extra_keys)}"
        )
    missing_keys = _DOCUMENT_DETAIL_REQUIRED_KEYS - set(detail)
    if missing_keys:
        raise ValueError(f"document event detail missing required keys: {sorted(missing_keys)}")

    if not isinstance(detail["hash"], str) or not detail["hash"]:
        raise ValueError("document event detail 'hash' must be a non-empty string")
    size = detail["size"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("document event detail 'size' must be a non-negative integer")
    extension = detail.get("extension")
    if extension is not None and not isinstance(extension, str):
        raise ValueError("document event detail 'extension' must be a string if present")


class Event(SQLModel, table=True):
    """Append-only audit log entry (spec §3.2, R4).

    `Event` rows are never updated or deleted: this class defines no such method, and none should
    ever be added to it or to any repository layer built on top of it. That's reinforced below by a
    `before_update` mapper listener that raises if an `UPDATE` is ever attempted against an
    existing row, so the invariant holds even if a future caller bypasses this module's own
    conventions. The document-detail restriction (R5) is enforced three ways: by `create_event()`
    (the sanctioned way to build one), by the `@validates` hook below (fires on every assignment
    *of `detail` itself* -- including reassignment after construction, e.g. someone swapping in
    forbidden content on an already-built document event), and by a `before_insert` listener that
    re-validates immediately before the row is written -- which is what catches an in-place
    mutation of `detail` (`event.detail["extra"] = "..."`) that neither `@validates` nor
    `before_update` would see, since that's neither an attribute assignment nor an UPDATE, and
    also what catches `kind` being set to a document kind *after* `detail` (the `@validates` hook
    deliberately does not fire on `kind` assignment, so that legitimate incremental construction --
    e.g. setting `detail` first, `kind` second, in separate statements -- isn't rejected mid-build;
    `before_insert` is the authoritative gate regardless of which field was set first or last).
    """

    id: int | None = Field(default=None, primary_key=True)
    case_id: int = Field(foreign_key="case.id")
    kind: str
    actor: str
    timestamp: datetime = Field(
        default_factory=_utcnow, sa_column=Column(UTCDateTime, nullable=False)
    )
    detail: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))

    @validates("detail")
    def _enforce_document_detail_restriction(self, key: str, value: Any) -> Any:
        """Re-run the R5 check whenever `detail` is assigned, not just at factory time.

        Fires via SQLAlchemy's attribute-instrumentation, so it runs on `Event(...)` construction
        (regardless of whether `create_event()` was used) and again if `detail` is reassigned
        afterwards -- there is no way to end up with a persisted document event whose `detail` has
        already been validated as `{hash, size, extension?}` and later swapped out.

        Deliberately does *not* also fire on `kind` assignment (see the class docstring): doing so
        would make validation ordering-dependent -- `Event(detail={})` followed by
        `e.kind = "document_received"` would raise even though the caller may still be mid-
        construction and intends to set a proper `detail` before flushing. `before_insert` below
        is what actually guarantees nothing invalid reaches the database, regardless of which
        field was set first.
        """
        kind = getattr(self, "kind", None)
        if kind in DOCUMENT_EVENT_KINDS and value is not None:
            validate_document_event_detail(value)
        return value


@sa_event.listens_for(Event, "before_insert", propagate=True)
def _validate_document_detail_on_insert(mapper: Any, connection: Any, target: Event) -> None:
    """Re-validate `detail` right before INSERT, closing an in-place-mutation gap in R5.

    The `@validates` hook above only fires on *assignment* (`event.detail = {...}`), not on
    in-place mutation of the dict it already holds (`event.detail["extra"] = "..."`). Someone
    could build a valid document `Event` via `create_event()`, then mutate `detail` in place
    before the first flush -- no `@validates` call is triggered by that, and `before_update`
    doesn't fire either, since this is a first-time INSERT, not an UPDATE. This listener runs
    immediately before the INSERT statement is emitted, so a mutated-in-place `detail` on a
    document event is still caught before it ever reaches the database.

    Unlike the `@validates` hook (which must tolerate `detail` being transiently unset while
    `kind` is validated during construction, before all fields are assigned), this listener runs
    after construction is complete, right before the INSERT -- there's no legitimate reason for
    `detail` to still be `None` here. Raise a clear `ValueError` rather than letting it fall
    through to a generic NOT NULL constraint violation from the database.
    """
    if target.kind in DOCUMENT_EVENT_KINDS:
        if target.detail is None:
            raise ValueError(
                "document event detail must not be None; provide {hash, size} at minimum"
            )
        validate_document_event_detail(target.detail)


@sa_event.listens_for(Event, "before_update", propagate=True)
def _reject_event_updates(mapper: Any, connection: Any, target: Event) -> None:
    """Block every `UPDATE` against an existing `Event` row at the SQLAlchemy level (R4).

    `Event` rows are insert-only: this listener fires for any flush that would emit an `UPDATE`
    statement for an `Event` (e.g. mutating a field on an already-persisted instance and calling
    `session.commit()`), and raises before the statement reaches the database. Inserting new
    `Event` rows is unaffected -- only updates to existing ones are rejected.
    """
    raise RuntimeError(
        "Event rows are append-only (R4): existing Event rows can never be updated, only inserted."
    )


def create_event(
    *,
    case_id: int,
    kind: str,
    actor: str,
    detail: dict[str, Any] | None = None,
    timestamp: datetime | None = None,
) -> Event:
    """Build an `Event`, validating the document-detail restriction (R5) for document event kinds.

    This is the sanctioned constructor for `Event` rows. For `kind in DOCUMENT_EVENT_KINDS`,
    `detail` is checked against `validate_document_event_detail()` before the `Event` is built.
    `Event`'s own `@validates` hook re-checks this regardless, so this isn't the only line of
    defense -- but going through `create_event()` is still the documented, sanctioned way to build
    one. A naive `timestamp` is rejected here rather than left to surface later as a `ValueError`
    from `UTCDateTime.process_bind_param()` at flush time, far from the actual mistake.
    """
    if timestamp is not None and timestamp.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware (UTC), got a naive datetime")
    resolved_detail: dict[str, Any] = dict(detail) if detail else {}
    if kind in DOCUMENT_EVENT_KINDS:
        if detail is None:
            raise ValueError(
                "document event detail must not be None; provide {hash, size} at minimum"
            )
        validate_document_event_detail(resolved_detail)
    return Event(
        case_id=case_id,
        kind=kind,
        actor=actor,
        detail=resolved_detail,
        timestamp=timestamp if timestamp is not None else _utcnow(),
    )
