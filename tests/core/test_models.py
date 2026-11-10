"""Tests for `rtbf_agent.core.models` (`Case`/`Event`, spec §3.2).

Covers the restrictions this module discharges (`docs/planning/RESTRICTIONS.md`):
R4 (append-only `Event`), R5 (no document content in `Event.detail`), R9 (indefinite waiting
states + per-case reminder override), R22 (per-case jurisdiction/drafting language, independent
of each other and of interface language).
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from rtbf_agent.core.models import (
    CASE_STATES_IN_ORDER,
    DOCUMENT_EVENT_KINDS,
    Case,
    CaseState,
    Event,
    create_event,
    validate_document_event_detail,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _make_case(**overrides) -> Case:
    defaults = {
        "site": "Example Site",
        "domain": "example.com",
        "target_email": "user@example.com",
    }
    defaults.update(overrides)
    return Case(**defaults)


# --- Case / kanban states -----------------------------------------------------------------


def test_case_state_enum_has_all_nine_kanban_states_in_order() -> None:
    assert [s.value for s in CASE_STATES_IN_ORDER] == [
        "Discovered",
        "Classified",
        "Draft ready",
        "Awaiting human approval",
        "Awaiting response",
        "Requires human action",
        "Deletion confirmed",
        "Rejected/Escalated",
        "Closed",
    ]


def test_case_default_state_is_discovered(session: Session) -> None:
    case = _make_case()
    session.add(case)
    session.commit()
    session.refresh(case)
    assert case.state == CaseState.DISCOVERED


def test_case_and_event_timestamps_survive_db_round_trip_with_timezone_intact(
    session: Session,
) -> None:
    # datetime.now(UTC) returns timezone-aware datetimes; a timezone-naive column would silently
    # strip that on write, and the read-back value would be unable to compare against a tz-aware
    # datetime without raising TypeError -- exactly the round-trip this test forces.
    case = _make_case()
    session.add(case)
    session.commit()
    session.refresh(case)

    event = create_event(case_id=case.id, kind="note", actor="agent", detail={})
    session.add(event)
    session.commit()
    session.refresh(event)

    assert case.discovered_at.tzinfo is not None
    assert case.updated_at.tzinfo is not None
    assert event.timestamp.tzinfo is not None
    # Would raise TypeError ("can't compare offset-naive and offset-aware datetimes") if the
    # timezone got stripped on the round trip through the database.
    assert case.discovered_at <= datetime.now(UTC)
    assert event.timestamp <= datetime.now(UTC)


def test_case_datetime_columns_reject_null_at_the_database_level(session: Session) -> None:
    # discovered_at/updated_at are non-Optional `datetime` in the type
    # annotation; the column must actually enforce that (nullable=False), or `case.x = None`
    # would be silently persisted and only surface later as a confusing AttributeError on
    # `.tzinfo` far from the assignment that caused it.
    case = _make_case()
    case.discovered_at = None  # type: ignore[assignment]
    session.add(case)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_create_event_rejects_naive_timestamp_with_a_clear_error() -> None:
    # A naive timestamp would otherwise surface as a confusing ValueError from
    # UTCDateTime.process_bind_param() at flush time, far from the actual caller mistake --
    # create_event() should fail fast with a clear message instead.
    with pytest.raises(ValueError, match="timezone-aware"):
        create_event(
            case_id=1,
            kind="note",
            actor="agent",
            detail={},
            timestamp=datetime(2026, 1, 1, 12, 0, 0),  # noqa: DTZ001 -- naive on purpose
        )


def test_case_transitions_through_all_ten_states_each_produce_an_event(session: Session) -> None:
    case = _make_case()
    session.add(case)
    session.commit()
    session.refresh(case)

    for state in CASE_STATES_IN_ORDER:
        case.state = state
        session.add(case)
        event = create_event(
            case_id=case.id,
            kind="case_state_changed",
            actor="agent",
            detail={"to": state.value},
        )
        session.add(event)
        session.commit()

    events = session.exec(select(Event).where(Event.case_id == case.id).order_by(Event.id)).all()
    assert len(events) == len(CASE_STATES_IN_ORDER)
    recorded_states = [e.detail["to"] for e in events]
    assert recorded_states == [s.value for s in CASE_STATES_IN_ORDER]


def test_case_in_waiting_state_has_no_timeout_field() -> None:
    # R9: no deadline/timeout field exists on Case at all -- waiting states can't expire because
    # there is nothing on the model that could hold a deadline.
    field_names = set(Case.model_fields)
    assert not any("timeout" in name.lower() or "deadline" in name.lower() for name in field_names)


def test_interface_language_has_no_per_case_override_field() -> None:
    # R22: interface language is a global-only setting -- guards against ever accidentally adding
    # a per-case column for it, which would conflate the three independent i18n axes.
    field_names = {name.lower() for name in Case.model_fields}
    assert not any("interface" in name for name in field_names)
    # Belt-and-suspenders: drafting_language is the only "language"-named field this model is
    # allowed to have -- an interface-language field would also match "language".
    language_fields = {name for name in field_names if "language" in name}
    assert language_fields == {"drafting_language"}


# --- Event append-only (R4) ----------------------------------------------------------------


def test_event_table_has_no_update_or_delete_path() -> None:
    forbidden_substrings = ("update", "delete", "modify", "edit")

    # Guard/enforcement machinery is expected to *mention* update/delete in its name (it exists to
    # block them) -- it is the opposite of a mutation path, so it's explicitly allowlisted here
    # rather than making the substring match so narrow it'd stop catching a real mutator method.
    allowlisted_names = {"_reject_event_updates"}

    event_own_members = {
        name for name in vars(Event) if not name.startswith("_") and name not in allowlisted_names
    }
    for name in event_own_members:
        lowered = name.lower()
        assert not any(word in lowered for word in forbidden_substrings), (
            f"Event defines {name!r}, which looks like a mutation/removal path"
        )

    import rtbf_agent.core.models as models_module

    module_functions = {
        name
        for name, obj in vars(models_module).items()
        if inspect.isfunction(obj)
        and obj.__module__ == models_module.__name__
        and name not in allowlisted_names
    }
    for name in module_functions:
        lowered = name.lower()
        assert not any(word in lowered for word in forbidden_substrings), (
            f"core/models.py defines {name!r}, which looks like an Event mutation/removal path"
        )


def test_event_row_update_is_rejected_at_the_orm_level(session: Session) -> None:
    # R4, enforced structurally (not just by naming convention): mutating an already-persisted
    # Event and flushing it must fail, regardless of which code path performed the mutation.
    case = _make_case()
    session.add(case)
    session.commit()
    session.refresh(case)

    event = create_event(case_id=case.id, kind="note", actor="agent", detail={"msg": "original"})
    session.add(event)
    session.commit()

    event.detail = {"msg": "tampered"}
    session.add(event)
    with pytest.raises(RuntimeError, match="append-only"):
        session.commit()
    session.rollback()


def test_event_constructed_directly_still_enforces_document_detail_restriction() -> None:
    # R5, closing the bypass where someone builds `Event(...)` instead of going through
    # `create_event()` -- the restriction must hold no matter which constructor is used.
    with pytest.raises(ValueError, match="extra keys"):
        Event(
            case_id=1,
            kind="document_received",
            actor="agent",
            detail={"hash": "abc", "size": 1, "extracted_text": "PII here"},
        )


def test_event_setting_kind_after_detail_does_not_eagerly_reject_incremental_construction() -> None:
    # Setting detail first, kind second (in separate statements) must not raise, even though
    # detail's current value ({}) would fail R5 validation on its own -- @validates deliberately
    # doesn't fire on kind assignment (see the class/method docstrings for why). before_insert
    # remains the authoritative gate, proven by the next assertion: flushing this half-built,
    # still-invalid event is still rejected before it reaches the database.
    event = Event(case_id=1, kind="", actor="agent", detail={})
    event.kind = "document_received"  # must not raise here
    assert event.kind == "document_received"
    assert event.detail == {}


def test_event_built_incrementally_with_still_invalid_detail_is_rejected_at_flush(
    session: Session,
) -> None:
    # Continuation of the test above: not raising at attribute-set time doesn't mean R5 is
    # weakened -- before_insert is still the authoritative gate, so this half-built event (kind
    # set, but detail was never filled in with real hash/size) is still rejected before it can
    # reach the database.
    case = _make_case()
    session.add(case)
    session.commit()
    session.refresh(case)

    event = Event(case_id=case.id, kind="", actor="agent", detail={})
    event.kind = "document_received"
    session.add(event)
    with pytest.raises(ValueError, match="missing required keys"):
        session.commit()
    session.rollback()


def test_event_reassigning_detail_after_construction_still_enforces_restriction() -> None:
    # R5: the check isn't only a one-time gate at __init__ -- reassigning `detail` on an
    # already-constructed (not yet persisted) Event re-triggers it too.
    event = create_event(
        case_id=1, kind="document_received", actor="agent", detail={"hash": "abc", "size": 1}
    )
    with pytest.raises(ValueError, match="extra keys"):
        event.detail = {"hash": "abc", "size": 1, "content": "raw bytes"}


def test_event_detail_mutated_in_place_before_first_flush_is_rejected_on_insert(
    session: Session,
) -> None:
    # R5: `@validates` only fires on attribute *assignment*, not on in-place mutation of the dict
    # already held by `detail`. This is exactly that gap -- mutate in place, then flush -- which
    # only the before_insert listener catches (before_update doesn't fire for a first-time INSERT).
    case = _make_case()
    session.add(case)
    session.commit()
    session.refresh(case)

    event = create_event(
        case_id=case.id, kind="document_received", actor="agent", detail={"hash": "abc", "size": 1}
    )
    event.detail["content"] = "raw PII bytes"  # in-place mutation, no @validates call triggered
    session.add(event)
    with pytest.raises(ValueError, match="extra keys"):
        session.commit()
    session.rollback()


def test_document_event_with_detail_forced_to_none_is_rejected_with_clear_error(
    session: Session,
) -> None:
    # A document event with detail=None would otherwise fall through to a generic NOT NULL
    # constraint violation from the database instead of a descriptive R5 error -- the
    # before_insert listener must catch it before the INSERT is even attempted.
    case = _make_case()
    session.add(case)
    session.commit()
    session.refresh(case)

    event = create_event(
        case_id=case.id, kind="document_received", actor="agent", detail={"hash": "abc", "size": 1}
    )
    event.detail = None  # type: ignore[assignment]
    session.add(event)
    with pytest.raises(ValueError, match="must not be None"):
        session.commit()
    session.rollback()


def test_event_requires_actor_kind_and_timestamp(session: Session) -> None:
    case = _make_case()
    session.add(case)
    session.commit()
    session.refresh(case)

    event = create_event(case_id=case.id, kind="case_state_changed", actor="agent", detail={})
    session.add(event)
    session.commit()
    session.refresh(event)

    assert event.actor == "agent"
    assert event.kind == "case_state_changed"
    assert event.timestamp is not None
    assert event.detail == {}


# --- Document event detail schema (R5) ------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(DOCUMENT_EVENT_KINDS))
def test_document_event_detail_accepts_hash_size_and_optional_extension(kind: str) -> None:
    event = create_event(
        case_id=1,
        kind=kind,
        actor="agent",
        detail={"hash": "abc123", "size": 1024, "extension": "pdf"},
    )
    assert event.detail == {"hash": "abc123", "size": 1024, "extension": "pdf"}

    event_no_extension = create_event(
        case_id=1,
        kind=kind,
        actor="agent",
        detail={"hash": "abc123", "size": 1024},
    )
    assert event_no_extension.detail == {"hash": "abc123", "size": 1024}


@pytest.mark.parametrize("kind", sorted(DOCUMENT_EVENT_KINDS))
def test_document_event_detail_schema_excludes_raw_content(kind: str) -> None:
    with pytest.raises(ValueError, match="extra keys"):
        create_event(
            case_id=1,
            kind=kind,
            actor="agent",
            detail={"hash": "abc123", "size": 1024, "content": "raw bytes here"},
        )


@pytest.mark.parametrize("kind", sorted(DOCUMENT_EVENT_KINDS))
def test_document_event_detail_requires_hash_and_size(kind: str) -> None:
    with pytest.raises(ValueError, match="missing required keys"):
        create_event(case_id=1, kind=kind, actor="agent", detail={"hash": "abc123"})


def test_document_event_never_contains_bytes_or_extracted_text() -> None:
    raw_document_bytes = b"%PDF-1.4 fake identity document content..."
    extracted_text = "John Doe, DOB 1990-01-01, passport number 123456789"

    for poisoned_detail in (
        {"hash": "abc", "size": 10, "raw_bytes": raw_document_bytes},
        {"hash": "abc", "size": 10, "content": raw_document_bytes},
        {"hash": "abc", "size": 10, "extracted_text": extracted_text},
        {"hash": "abc", "size": 10, "base64": "ZmFrZSBkb2N1bWVudA=="},
    ):
        with pytest.raises(ValueError):
            create_event(
                case_id=1,
                kind="document_received",
                actor="agent",
                detail=poisoned_detail,
            )


def test_non_document_event_kind_is_not_restricted_to_hash_size_extension() -> None:
    # Sanity check the restriction is scoped to document event kinds only -- an unrelated kind
    # (e.g. a state-change note) is free to carry other fields.
    event = create_event(
        case_id=1,
        kind="case_state_changed",
        actor="agent",
        detail={"from": "Discovered", "to": "Classified", "note": "auto-classified"},
    )
    assert event.detail["note"] == "auto-classified"


def test_validate_document_event_detail_directly() -> None:
    validate_document_event_detail({"hash": "abc", "size": 1})
    validate_document_event_detail({"hash": "abc", "size": 1, "extension": "png"})
    with pytest.raises(ValueError):
        validate_document_event_detail({"hash": "abc", "size": 1, "content": b"x"})
    with pytest.raises(ValueError):
        validate_document_event_detail({"size": 1})


def test_validate_document_event_detail_rejects_wrong_value_types() -> None:
    # key presence isn't enough -- values must actually be the types callers rely on.
    with pytest.raises(ValueError):
        validate_document_event_detail({"hash": 123, "size": 1})
    with pytest.raises(ValueError):
        validate_document_event_detail({"hash": "", "size": 1})
    with pytest.raises(ValueError):
        validate_document_event_detail({"hash": "abc", "size": "ten"})
    with pytest.raises(ValueError):
        validate_document_event_detail({"hash": "abc", "size": -1})
    with pytest.raises(ValueError):
        validate_document_event_detail({"hash": "abc", "size": True})
    with pytest.raises(ValueError):
        validate_document_event_detail({"hash": "abc", "size": 1, "extension": 42})


# --- Per-case reminder override (R9) --------------------------------------------------------


def test_case_reminder_fields_default_to_none_deferring_to_global() -> None:
    case = _make_case()
    assert case.reminder_enabled is None
    assert case.reminder_interval_days is None


def test_case_level_reminder_config_overrides_global_default() -> None:
    case = _make_case(reminder_enabled=True, reminder_interval_days=10)
    assert case.resolved_reminder_enabled(global_default=False) is True
    assert case.resolved_reminder_interval_days(global_default=None) == 10


def test_case_can_opt_out_of_reminder_when_global_default_is_on() -> None:
    case = _make_case(reminder_enabled=False)
    assert case.resolved_reminder_enabled(global_default=True) is False


def test_case_without_override_falls_back_to_global_default() -> None:
    case = _make_case()
    assert case.resolved_reminder_enabled(global_default=True) is True
    assert case.resolved_reminder_enabled(global_default=False) is False
    assert case.resolved_reminder_interval_days(global_default=30) == 30


# --- Per-case jurisdiction / drafting language override (R22) ------------------------------


def test_case_level_drafting_language_and_jurisdiction_override_global_defaults_independently() -> (
    None
):
    # Overriding jurisdiction alone must not affect the resolved drafting language, and vice
    # versa -- the two axes never derive from one another.
    case_overrides_jurisdiction_only = _make_case(applicable_jurisdiction="GDPR")
    assert (
        case_overrides_jurisdiction_only.resolved_applicable_jurisdiction(global_default="CCPA")
        == "GDPR"
    )
    assert case_overrides_jurisdiction_only.resolved_drafting_language(global_default="en") == "en"

    case_overrides_language_only = _make_case(drafting_language="ja")
    assert case_overrides_language_only.resolved_drafting_language(global_default="en") == "ja"
    assert (
        case_overrides_language_only.resolved_applicable_jurisdiction(global_default="GDPR")
        == "GDPR"
    )

    case_overrides_both = _make_case(applicable_jurisdiction="LGPD", drafting_language="pt")
    assert case_overrides_both.resolved_applicable_jurisdiction(global_default="GDPR") == "LGPD"
    assert case_overrides_both.resolved_drafting_language(global_default="en") == "pt"


def test_case_jurisdiction_and_language_default_to_none_deferring_to_global() -> None:
    case = _make_case()
    assert case.applicable_jurisdiction is None
    assert case.drafting_language is None
    assert case.resolved_applicable_jurisdiction(global_default="GDPR") == "GDPR"
    assert case.resolved_drafting_language(global_default="en") == "en"


def test_all_combinations_of_case_level_jurisdiction_and_language_are_valid() -> None:
    jurisdictions = ["GDPR", "CCPA", "LGPD", None]
    languages = ["es", "en", "ja", None]
    for jurisdiction in jurisdictions:
        for language in languages:
            case = _make_case(applicable_jurisdiction=jurisdiction, drafting_language=language)
            assert case.applicable_jurisdiction == jurisdiction
            assert case.drafting_language == language


def test_case_recipient_email_defaults_to_none(session: Session) -> None:
    """`recipient_email` (the site's real deletion-request contact, issue #52) is nullable and
    defaults to `None` -- discovery can't determine it, so a freshly-created `Case` has no
    recipient until one is set manually before send. It is a plain nullable field, deliberately
    *not* an override of a global default like jurisdiction/language: there is no `resolved_*`
    helper, because there is nothing global to fall back to (a send with no recipient is refused,
    never defaulted -- see agent_loop)."""
    case = _make_case()
    assert case.recipient_email is None

    session.add(case)
    session.commit()
    session.refresh(case)
    assert case.recipient_email is None


def test_case_recipient_email_is_settable_and_distinct_from_target_email(session: Session) -> None:
    """`recipient_email` and `target_email` are independent fields: the recipient (the company's
    privacy contact) is where a request is sent; `target_email` is the user's own scanned mailbox
    the account was discovered in. Setting one must never affect the other (issue #52)."""
    case = _make_case(
        target_email="me@personal.example", recipient_email="dpo@some-company.example"
    )
    session.add(case)
    session.commit()
    session.refresh(case)

    assert case.target_email == "me@personal.example"
    assert case.recipient_email == "dpo@some-company.example"
