"""Tests for the shared decision-recording layer (`rtbf_agent.case_actions`) -- the module every
interface (kanban routes, CLI subcommands, later Telegram buttons) delegates to, so a decision is
recorded identically no matter which door it came through (see `tasks/todo.md`'s "Automated case
loop" section for the full design)."""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, col, create_engine, select

from rtbf_agent import case_actions
from rtbf_agent.case_actions import (
    CaseNotFoundError,
    InvalidCaseStateError,
    InvalidInputError,
)
from rtbf_agent.core.models import Case, CaseState, Document, DocumentStatus, Event, create_event


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


def _make_case(session: Session, *, state: CaseState = CaseState.DRAFT_READY, **kwargs) -> Case:
    case = Case(
        site="Example Corp",
        domain="example.com",
        target_email="user@example.com",
        state=state,
        **kwargs,
    )
    session.add(case)
    session.commit()
    session.refresh(case)
    return case


def _events(session: Session, case_id: int, kind: str) -> list[Event]:
    return list(
        session.exec(select(Event).where(Event.case_id == case_id, Event.kind == kind)).all()
    )


def _add_draft_event(
    session: Session,
    case: Case,
    draft: str = "Please delete my data.",
    subject: str = "Deletion request",
) -> None:
    """Mirrors `test_agent_loop_mock.py`'s helper of the same name field-for-field (OCR review
    finding: an earlier version of this helper used a different default `subject` and a `None`
    return type, undermining the "mirrors" claim -- kept in sync now rather than just similar) --
    see that module's docstring for the full rationale: a case constructed directly in `Draft
    ready` needs a `draft_proposed` `Event` manually added, since real cases only reach that state
    via `_draft()`/`case_actions.approve_draft()` (from a `Requires human action` round with a
    pending preview draft, per the drafting-is-a-preview redesign), both of which already write
    one."""
    assert case.id is not None
    session.add(
        create_event(
            case_id=case.id,
            kind="draft_proposed",
            actor="agent",
            detail={"draft": draft, "subject": subject},
        )
    )
    session.commit()


def _record_approval(session: Session, case: Case) -> Event:
    """Record an `approval_granted` `Event` directly -- the test-side stand-in for
    `case_actions.approve_draft()`. Mirrors `test_agent_loop_mock.py`'s helper of the same name
    field-for-field (OCR review finding, see `_add_draft_event()`'s docstring above)."""
    assert case.id is not None
    event = create_event(
        case_id=case.id,
        kind="approval_granted",
        actor="human",
        detail={"action": "send_request"},
    )
    session.add(event)
    session.commit()
    return event


# --------------------------------------------------------------------------------------------- #
# confirm_recipient
# --------------------------------------------------------------------------------------------- #


def test_confirm_recipient_sets_the_address_and_records_an_event(session: Session) -> None:
    case = _make_case(session)
    assert case.id is not None

    result = case_actions.confirm_recipient(session, case.id, "privacy@example.com")

    assert result.recipient_email == "privacy@example.com"
    events = _events(session, case.id, "recipient_confirmed")
    assert len(events) == 1
    assert events[0].detail == {"recipient": "privacy@example.com", "matched_suggestion": False}


def test_confirm_recipient_rejects_a_malformed_address(session: Session) -> None:
    case = _make_case(session)
    assert case.id is not None

    with pytest.raises(InvalidInputError):
        case_actions.confirm_recipient(session, case.id, "not-an-email")


def test_confirm_recipient_refuses_once_already_approved(session: Session) -> None:
    case = _make_case(session, recipient_email="approved@example.com")
    assert case.id is not None
    session.add(
        create_event(
            case_id=case.id,
            kind="approval_granted",
            actor="human",
            detail={"action": "send_request"},
        )
    )
    session.commit()

    with pytest.raises(InvalidCaseStateError):
        case_actions.confirm_recipient(session, case.id, "different@example.com")


def test_confirm_recipient_404_for_unknown_case(session: Session) -> None:
    with pytest.raises(CaseNotFoundError):
        case_actions.confirm_recipient(session, 999999, "a@example.com")


# --------------------------------------------------------------------------------------------- #
# set_case_jurisdiction / set_case_drafting_language (no-prompt-at-creation reversal's review/
# override path -- only editable while a Case is still Discovered, before _classify() locks these
# fields in)
# --------------------------------------------------------------------------------------------- #


def test_set_case_jurisdiction_sets_the_field_and_records_an_event(session: Session) -> None:
    case = _make_case(session, state=CaseState.DISCOVERED)
    assert case.id is not None

    result = case_actions.set_case_jurisdiction(session, case.id, "GDPR")

    assert result.applicable_jurisdiction == "GDPR"
    events = _events(session, case.id, "jurisdiction_overridden")
    assert len(events) == 1
    assert events[0].actor == "human"
    assert events[0].detail == {"value": "GDPR"}


def test_set_case_jurisdiction_strips_whitespace(session: Session) -> None:
    case = _make_case(session, state=CaseState.DISCOVERED)
    assert case.id is not None

    result = case_actions.set_case_jurisdiction(session, case.id, "  GDPR  ")

    assert result.applicable_jurisdiction == "GDPR"


def test_set_case_jurisdiction_rejects_blank_input(session: Session) -> None:
    case = _make_case(session, state=CaseState.DISCOVERED)
    assert case.id is not None

    with pytest.raises(InvalidInputError):
        case_actions.set_case_jurisdiction(session, case.id, "   ")


@pytest.mark.parametrize(
    "state",
    [s for s in CaseState if s != CaseState.DISCOVERED],
)
def test_set_case_jurisdiction_rejects_every_state_other_than_discovered(
    session: Session, state: CaseState
) -> None:
    case = _make_case(session, state=state)
    assert case.id is not None

    with pytest.raises(InvalidCaseStateError):
        case_actions.set_case_jurisdiction(session, case.id, "GDPR")


def test_set_case_jurisdiction_404_for_unknown_case(session: Session) -> None:
    with pytest.raises(CaseNotFoundError):
        case_actions.set_case_jurisdiction(session, 999999, "GDPR")


def test_set_case_drafting_language_sets_the_field_and_records_an_event(session: Session) -> None:
    case = _make_case(session, state=CaseState.DISCOVERED)
    assert case.id is not None

    result = case_actions.set_case_drafting_language(session, case.id, "fr")

    assert result.drafting_language == "fr"
    events = _events(session, case.id, "drafting_language_overridden")
    assert len(events) == 1
    assert events[0].actor == "human"
    assert events[0].detail == {"value": "fr"}


def test_set_case_drafting_language_rejects_blank_input(session: Session) -> None:
    case = _make_case(session, state=CaseState.DISCOVERED)
    assert case.id is not None

    with pytest.raises(InvalidInputError):
        case_actions.set_case_drafting_language(session, case.id, "")


@pytest.mark.parametrize(
    "state",
    [s for s in CaseState if s != CaseState.DISCOVERED],
)
def test_set_case_drafting_language_rejects_every_state_other_than_discovered(
    session: Session, state: CaseState
) -> None:
    case = _make_case(session, state=state)
    assert case.id is not None

    with pytest.raises(InvalidCaseStateError):
        case_actions.set_case_drafting_language(session, case.id, "fr")


def test_set_case_drafting_language_404_for_unknown_case(session: Session) -> None:
    with pytest.raises(CaseNotFoundError):
        case_actions.set_case_drafting_language(session, 999999, "fr")


def test_set_case_jurisdiction_and_drafting_language_are_independent(session: Session) -> None:
    """R22: setting one never touches the other."""
    case = _make_case(session, state=CaseState.DISCOVERED)
    assert case.id is not None

    case_actions.set_case_jurisdiction(session, case.id, "GDPR")
    session.refresh(case)
    assert case.drafting_language is None

    case_actions.set_case_drafting_language(session, case.id, "fr")
    session.refresh(case)
    assert case.applicable_jurisdiction == "GDPR"  # untouched by the drafting-language call


# --------------------------------------------------------------------------------------------- #
# approve_draft / reject_draft
# --------------------------------------------------------------------------------------------- #


def test_approve_draft_records_approval_granted_without_sending(session: Session) -> None:
    case = _make_case(session)
    assert case.id is not None

    result = case_actions.approve_draft(session, case.id)

    assert result.state == CaseState.DRAFT_READY  # unchanged -- never sends
    assert len(_events(session, case.id, "approval_granted")) == 1


def test_reject_draft_moves_to_rejected_escalated(session: Session) -> None:
    case = _make_case(session)
    assert case.id is not None

    result = case_actions.reject_draft(session, case.id)

    assert result.state == CaseState.REJECTED_ESCALATED
    assert len(_events(session, case.id, "approval_rejected")) == 1


def test_approve_draft_wrong_state(session: Session) -> None:
    case = _make_case(session, state=CaseState.DISCOVERED)
    assert case.id is not None
    with pytest.raises(InvalidCaseStateError):
        case_actions.approve_draft(session, case.id)


# --------------------------------------------------------------------------------------------- #
# confirm_followup / dispute_followup
# --------------------------------------------------------------------------------------------- #


def _classified_case(session: Session, intent: str) -> Case:
    case = _make_case(session, state=CaseState.AWAITING_RESPONSE, recipient_email="p@example.com")
    assert case.id is not None
    session.add(
        create_event(
            case_id=case.id,
            kind="response_classified",
            actor="agent",
            detail={"message_id": "m-1", "intent": intent, "reasoning": "because"},
        )
    )
    session.commit()
    return case


def test_confirm_followup_rejection_intent_moves_to_rejected_escalated(session: Session) -> None:
    case = _classified_case(session, "rejection")
    assert case.id is not None

    result = case_actions.confirm_followup(session, case.id)

    assert result.state == CaseState.REJECTED_ESCALATED
    assert len(_events(session, case.id, "followup_confirmed")) == 1


def test_confirm_followup_refuses_a_pending_confirmation_intent(session: Session) -> None:
    """Maintainer-directed narrowing of issue #68 (see `followup.py`'s module docstring, "Why
    CONFIRMATION auto-closes now"): `follow_up()` now resolves a CONFIRMATION classification
    itself, so a case is never left in `Awaiting response` with a pending *confirmation* decision
    for `confirm_followup()`/`dispute_followup()` to act on -- only REJECTION reaches here."""
    case = _classified_case(session, "confirmation")
    assert case.id is not None
    with pytest.raises(InvalidCaseStateError):
        case_actions.confirm_followup(session, case.id)
    with pytest.raises(InvalidCaseStateError):
        case_actions.dispute_followup(session, case.id)


def test_dispute_followup_moves_to_requires_human_action(session: Session) -> None:
    case = _classified_case(session, "rejection")
    assert case.id is not None

    result = case_actions.dispute_followup(session, case.id)

    assert result.state == CaseState.REQUIRES_HUMAN_ACTION
    assert len(_events(session, case.id, "followup_disputed")) == 1


def test_confirm_followup_refuses_with_no_pending_classification(session: Session) -> None:
    case = _make_case(session, state=CaseState.AWAITING_RESPONSE)
    assert case.id is not None
    with pytest.raises(InvalidCaseStateError):
        case_actions.confirm_followup(session, case.id)


def test_confirm_followup_refuses_an_already_consumed_classification(session: Session) -> None:
    case = _classified_case(session, "rejection")
    assert case.id is not None
    case_actions.confirm_followup(session, case.id)

    # Case is now terminal; a second attempt must not find an actionable classification, since
    # the case is no longer even in Awaiting response.
    with pytest.raises(InvalidCaseStateError):
        case_actions.confirm_followup(session, case.id)


def test_confirm_followup_wrong_state(session: Session) -> None:
    case = _make_case(session, state=CaseState.DISCOVERED)
    assert case.id is not None
    with pytest.raises(InvalidCaseStateError):
        case_actions.confirm_followup(session, case.id)


# --------------------------------------------------------------------------------------------- #
# reopen_case
# --------------------------------------------------------------------------------------------- #


def test_reopen_case_moves_deletion_confirmed_to_requires_human_action(session: Session) -> None:
    """The mitigating control for `follow_up()`'s CONFIRMATION auto-close: a human can always undo
    a wrongly auto-closed case."""
    case = _make_case(session, state=CaseState.DELETION_CONFIRMED)
    assert case.id is not None

    result = case_actions.reopen_case(session, case.id)

    assert result.state == CaseState.REQUIRES_HUMAN_ACTION
    [event] = _events(session, case.id, "case_reopened")
    assert event.actor == "human"
    assert event.detail["from_state"] == CaseState.DELETION_CONFIRMED.value
    assert event.detail["to_state"] == CaseState.REQUIRES_HUMAN_ACTION.value


@pytest.mark.parametrize(
    "state",
    [
        CaseState.DISCOVERED,
        CaseState.CLASSIFIED,
        CaseState.DRAFT_READY,
        CaseState.AWAITING_HUMAN_APPROVAL,
        CaseState.AWAITING_RESPONSE,
        CaseState.REQUIRES_HUMAN_ACTION,
        CaseState.REJECTED_ESCALATED,
        CaseState.CLOSED,
    ],
)
def test_reopen_case_refuses_any_state_other_than_deletion_confirmed(
    session: Session, state: CaseState
) -> None:
    """Out of scope per the maintainer's decision: reopening a `Rejected/Escalated` case (or any
    other non-`Deletion confirmed` state) is not this action's job."""
    case = _make_case(session, state=state)
    assert case.id is not None
    with pytest.raises(InvalidCaseStateError):
        case_actions.reopen_case(session, case.id)


def test_reopen_case_refuses_a_nonexistent_case(session: Session) -> None:
    with pytest.raises(CaseNotFoundError):
        case_actions.reopen_case(session, 999999)


# --------------------------------------------------------------------------------------------- #
# provide_text_response / attach_document
# --------------------------------------------------------------------------------------------- #


def test_provide_text_response_records_an_event(session: Session) -> None:
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None

    case_actions.provide_text_response(session, case.id, "Here is my date of birth: 1990-01-01")

    events = _events(session, case.id, "additional_info_text_provided")
    assert len(events) == 1
    assert events[0].detail == {"text": "Here is my date of birth: 1990-01-01"}


def test_provide_text_response_rejects_blank_text(session: Session) -> None:
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None
    with pytest.raises(InvalidInputError):
        case_actions.provide_text_response(session, case.id, "   ")


def test_provide_text_response_rejects_text_over_the_length_limit(session: Session) -> None:
    """OCR review finding: matches the kanban form's own `maxlength` -- a backstop against an
    accidental paste of something enormous ending up verbatim in the audit trail, since the CLI's
    `provide-text` subcommand has no HTML form to enforce a limit client-side."""
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None
    with pytest.raises(InvalidInputError):
        case_actions.provide_text_response(
            session, case.id, "x" * (case_actions._MAX_TEXT_RESPONSE_CHARS + 1)
        )


def test_provide_text_response_wrong_state(session: Session) -> None:
    case = _make_case(session, state=CaseState.DRAFT_READY)
    assert case.id is not None
    with pytest.raises(InvalidCaseStateError):
        case_actions.provide_text_response(session, case.id, "hello")


# --------------------------------------------------------------------------------------------- #
# approve_draft / reject_draft / edit_draft -- widened gate: also usable from a `Requires human
# action` case with a pending preview draft (the drafting-is-a-preview redesign that superseded
# issue #117/#119's fused draft+send -- see `followup.py`'s module docstring), not just `Draft
# ready`.
# --------------------------------------------------------------------------------------------- #


def _make_case_with_pending_additional_info_draft(session: Session) -> Case:
    """A `Case` in `Requires human action` with a preview draft already composed for the current
    round -- exactly as `followup.draft_additional_info_response()` would leave it. The new origin
    `case_actions._send_eligible_case()` accepts alongside the original `Draft ready` one."""
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None
    session.add(create_event(case_id=case.id, kind="additional_info_requested", actor="agent"))
    session.commit()
    _add_draft_event(session, case, draft="a previewed reply", subject="Deletion request")
    return case


def test_approve_draft_from_a_pending_additional_info_draft_flips_state_to_draft_ready(
    session: Session,
) -> None:
    """The one moment `case.state` actually changes for this new origin (`approve_draft()`'s own
    docstring): approving a case still nominally `Requires human action` moves it to `Draft ready`
    as part of the same write as the `approval_granted` Event, handing off to the existing,
    unmodified recurring send loop exactly like any first-time draft."""
    case = _make_case_with_pending_additional_info_draft(session)
    assert case.id is not None

    result = case_actions.approve_draft(session, case.id)

    assert result.state == CaseState.DRAFT_READY
    assert len(_events(session, case.id, "approval_granted")) == 1


def test_approve_draft_from_draft_ready_origin_is_still_a_no_op_on_state(session: Session) -> None:
    """Regression coverage: the original `Draft ready` origin is unaffected by the widened gate."""
    case = _make_case(session)  # defaults to Draft ready
    assert case.id is not None

    result = case_actions.approve_draft(session, case.id)

    assert result.state == CaseState.DRAFT_READY
    assert len(_events(session, case.id, "approval_granted")) == 1


def test_reject_draft_from_a_pending_additional_info_draft_moves_to_rejected_escalated(
    session: Session,
) -> None:
    case = _make_case_with_pending_additional_info_draft(session)
    assert case.id is not None

    result = case_actions.reject_draft(session, case.id)

    assert result.state == CaseState.REJECTED_ESCALATED
    assert len(_events(session, case.id, "approval_rejected")) == 1


def test_edit_draft_from_a_pending_additional_info_draft_overwrites_body_and_keeps_state(
    session: Session,
) -> None:
    case = _make_case_with_pending_additional_info_draft(session)
    assert case.id is not None

    result = case_actions.edit_draft(session, case.id, "hand-edited preview")

    assert result.state == CaseState.REQUIRES_HUMAN_ACTION
    draft_events = _events(session, case.id, "draft_proposed")
    assert len(draft_events) == 2  # the composed preview, plus this hand-edit
    assert max(draft_events, key=lambda e: e.id or 0).detail["draft"] == "hand-edited preview"


def test_approve_reject_edit_refuse_requires_human_action_with_no_pending_draft(
    session: Session,
) -> None:
    """Neither eligible origin holds: `Requires human action` with no `draft_proposed` Event at all
    for the current round is not send/reject/edit-eligible."""
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None

    with pytest.raises(InvalidCaseStateError):
        case_actions.approve_draft(session, case.id)
    with pytest.raises(InvalidCaseStateError):
        case_actions.reject_draft(session, case.id)
    with pytest.raises(InvalidCaseStateError):
        case_actions.edit_draft(session, case.id, "hand-edited preview")


def test_approve_reject_edit_refuse_a_pending_draft_left_over_from_an_earlier_round(
    session: Session,
) -> None:
    """A `draft_proposed` Event recorded *before* the current round's `additional_info_requested`
    Event (i.e. from an earlier, already-resolved round) must not make a case eligible for a round
    it was never drafted for -- mirrors `_has_pending_additional_info_draft()`'s freshness rule."""
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None
    _add_draft_event(session, case, draft="an earlier round's stale draft")
    session.add(create_event(case_id=case.id, kind="additional_info_requested", actor="agent"))
    session.commit()

    with pytest.raises(InvalidCaseStateError):
        case_actions.approve_draft(session, case.id)
    with pytest.raises(InvalidCaseStateError):
        case_actions.reject_draft(session, case.id)
    with pytest.raises(InvalidCaseStateError):
        case_actions.edit_draft(session, case.id, "hand-edited preview")


def test_approve_reject_edit_refuse_a_draft_already_consumed_by_an_earlier_approval(
    session: Session,
) -> None:
    """Code-reviewer finding on this PR: a case that completed a full additional-info round
    (drafted, approved, sent) and *later* returns to `Requires human action` via
    `dispute_followup()`/`reopen_case()` -- neither of which records a fresh
    `additional_info_requested` Event -- must not have its old, already-approved-and-sent
    `draft_proposed` Event mistaken for a fresh, pending preview just because no newer
    `additional_info_requested` Event exists to compare it against. Without this check, approving
    here would silently resend stale, unrelated content under a brand new approval."""
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None
    session.add(create_event(case_id=case.id, kind="additional_info_requested", actor="agent"))
    session.commit()
    _add_draft_event(session, case, draft="a previewed reply, already approved and sent")
    # The round completed for real: approved, then sent (request_sent after the approval).
    _record_approval(session, case)
    session.add(create_event(case_id=case.id, kind="request_sent", actor="agent"))
    # Back to Requires human action via a path that records no fresh additional_info_requested
    # Event -- dispute_followup()/reopen_case()'s exact shape.
    case.state = CaseState.REQUIRES_HUMAN_ACTION
    session.add(case)
    session.commit()

    with pytest.raises(InvalidCaseStateError):
        case_actions.approve_draft(session, case.id)
    with pytest.raises(InvalidCaseStateError):
        case_actions.reject_draft(session, case.id)
    with pytest.raises(InvalidCaseStateError):
        case_actions.edit_draft(session, case.id, "hand-edited preview")


# --------------------------------------------------------------------------------------------- #
# edit_draft (LLM-drafted-replies extension)
# --------------------------------------------------------------------------------------------- #


def test_edit_draft_records_a_new_draft_proposed_event_with_the_edited_body(
    session: Session,
) -> None:
    case = _make_case(session, state=CaseState.DRAFT_READY)
    assert case.id is not None
    _add_draft_event(session, case, draft="original body", subject="original subject")

    result = case_actions.edit_draft(session, case.id, "hand-edited body")

    assert result.state == CaseState.DRAFT_READY  # unchanged
    draft_events = _events(session, case.id, "draft_proposed")
    assert len(draft_events) == 2  # the original, plus this edit's
    latest = max(draft_events, key=lambda event: event.id or 0)
    assert latest.detail["draft"] == "hand-edited body"
    assert latest.actor == "human"


def test_edit_draft_defaults_subject_to_the_current_drafts_subject(session: Session) -> None:
    case = _make_case(session, state=CaseState.DRAFT_READY)
    assert case.id is not None
    _add_draft_event(session, case, draft="original body", subject="original subject")

    case_actions.edit_draft(session, case.id, "hand-edited body")

    draft_events = _events(session, case.id, "draft_proposed")
    latest = max(draft_events, key=lambda event: event.id or 0)
    assert latest.detail["subject"] == "original subject"


def test_edit_draft_accepts_an_explicit_subject(session: Session) -> None:
    case = _make_case(session, state=CaseState.DRAFT_READY)
    assert case.id is not None
    _add_draft_event(session, case, draft="original body", subject="original subject")

    case_actions.edit_draft(session, case.id, "hand-edited body", subject="new subject")

    draft_events = _events(session, case.id, "draft_proposed")
    latest = max(draft_events, key=lambda event: event.id or 0)
    assert latest.detail["subject"] == "new subject"


def test_edit_draft_records_the_body_verbatim(session: Session) -> None:
    """`case_actions.py`'s own module docstring: DB-only, no LLM calls -- `edit_draft()` records
    exactly what the human typed, verbatim, never passes it through any drafting step. This only
    checks the recorded text matches byte-for-byte (OCR review finding: renamed from the previous
    `..._never_calls_an_llm` name, which overclaimed -- `edit_draft()` takes no `LLMProvider`
    parameter at all, so there is nothing to mock/assert-not-called here; the "no LLM" guarantee is
    structural (the signature has no such parameter), not something a black-box test can prove)."""
    case = _make_case(session, state=CaseState.DRAFT_READY)
    assert case.id is not None
    _add_draft_event(session, case)

    case_actions.edit_draft(session, case.id, "please delete my account, verbatim text")

    draft_events = _events(session, case.id, "draft_proposed")
    latest = max(draft_events, key=lambda event: event.id or 0)
    assert latest.detail["draft"] == "please delete my account, verbatim text"


def test_edit_draft_rejects_blank_body(session: Session) -> None:
    case = _make_case(session, state=CaseState.DRAFT_READY)
    assert case.id is not None
    _add_draft_event(session, case)
    with pytest.raises(InvalidInputError):
        case_actions.edit_draft(session, case.id, "   ")


def test_edit_draft_refused_outside_draft_ready(session: Session) -> None:
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None
    with pytest.raises(InvalidCaseStateError):
        case_actions.edit_draft(session, case.id, "hand-edited body")


def test_edit_draft_refused_once_approved(session: Session) -> None:
    """R1: mirrors `confirm_recipient()`'s "refuse once approved" precedent -- editing after
    approval would silently apply the edit to an already-authorized send, since `send_request()`
    reads the latest `draft_proposed` `Event` fresh at send time rather than snapshotting it."""
    case = _make_case(session, state=CaseState.DRAFT_READY)
    assert case.id is not None
    _add_draft_event(session, case)
    _record_approval(session, case)

    with pytest.raises(InvalidCaseStateError):
        case_actions.edit_draft(session, case.id, "hand-edited body")


def test_edit_draft_refused_when_an_approval_races_in_after_the_initial_check(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OCR review finding (R1): the initial gate check and the `Event` write are two separate
    round-trips -- a concurrent `approve_draft()` call could commit an approval in between them
    (this app's kanban routes run in a worker thread pool, each with its own `Session`). Simulates
    that race by making `_has_unconsumed_approval_event()` answer "not yet approved" on its first
    call (the initial gate) and "approved" on its second (the flush-then-recheck this fix adds),
    without actually needing two real threads: proves the second check is real, not a no-op, and
    that a detected race rolls back cleanly -- no orphan `draft_proposed` `Event` left behind."""
    case = _make_case(session, state=CaseState.DRAFT_READY)
    assert case.id is not None
    _add_draft_event(session, case, draft="original body", subject="original subject")

    # Answers "not yet approved" the first time (the initial gate) and "approved" the second (the
    # flush-then-recheck this fix adds) -- standing in for a concurrent approve_draft() call that
    # committed for real, on its own connection, in the gap between the two checks (not simulated
    # via a second real session here: this project's test engines are in-memory SQLite, where a
    # second connection would see an empty, unrelated database, not this test's actual data -- the
    # property under test is "a True second answer causes a rollback + raise", independent of
    # which real-world mechanism produces that answer).
    call_count = 0

    def _racing_check(session_arg: Session, case_id_arg: int) -> bool:
        nonlocal call_count
        call_count += 1
        return call_count > 1

    monkeypatch.setattr(case_actions, "_has_unconsumed_approval_event", _racing_check)

    with pytest.raises(InvalidCaseStateError):
        case_actions.edit_draft(session, case.id, "hand-edited body")

    assert call_count == 2  # the re-check actually ran, not just the initial gate
    draft_events = _events(session, case.id, "draft_proposed")
    assert len(draft_events) == 1  # only the original -- the racing edit was rolled back
    assert draft_events[0].detail["draft"] == "original body"


def test_select_documents_for_resend_refused_when_an_approval_races_in_after_the_initial_check(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same race, same fix, applied to `select_documents_for_resend()` -- see
    `test_edit_draft_refused_when_an_approval_races_in_after_the_initial_check()` above."""
    origin_case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert origin_case.id is not None
    case_actions.attach_document(session, origin_case.id, b"id card", "image/png")
    document = session.exec(select(Document)).one()

    case = _make_case(session, state=CaseState.DRAFT_READY)
    assert case.id is not None
    _add_draft_event(session, case)

    # See test_edit_draft_refused_when_an_approval_races_in_after_the_initial_check() above for why
    # this is a direct False-then-True stand-in rather than a second real concurrent session.
    call_count = 0

    def _racing_check(session_arg: Session, case_id_arg: int) -> bool:
        nonlocal call_count
        call_count += 1
        return call_count > 1

    monkeypatch.setattr(case_actions, "_has_unconsumed_approval_event", _racing_check)

    with pytest.raises(InvalidCaseStateError):
        case_actions.select_documents_for_resend(session, case.id, [document.id])

    assert call_count == 2
    assert _events(session, case.id, "documents_selected_for_resend") == []


def test_edit_draft_404_for_unknown_case(session: Session) -> None:
    with pytest.raises(CaseNotFoundError):
        case_actions.edit_draft(session, 999999, "hand-edited body")


@pytest.mark.parametrize(
    "event_kinds",
    [
        (),  # no approval ever recorded
        ("approval_granted",),  # approved, never sent -- still pending
        ("approval_granted", "request_sent"),  # approved and sent -- consumed
        (
            "approval_granted",
            "request_sent",
            "approval_granted",
        ),  # round 2 approved -- pending again
        (
            "approval_granted",
            "request_sent",
            "approval_granted",
            "request_sent",
        ),  # round 2 also sent -- consumed
    ],
)
def test_has_unconsumed_approval_event_matches_agent_loops_identical_staleness_check(
    session: Session, event_kinds: tuple[str, ...]
) -> None:
    """`case_actions._has_unconsumed_approval_event()` is a deliberate duplicate of
    `agent_loop._existing_approval_granted_event()`'s staleness logic (not a cross-module import --
    see `case_actions.py`'s own module docstring on why it doesn't delegate gating to another
    module). Code review (this PR) flagged that duplication as a real drift risk: a fix applied to
    one copy and missed in the other would silently reintroduce an R1 gate bug. This pins both
    functions to agree across a small matrix of event histories, so any future divergence fails a
    test instead of silently shipping."""
    from rtbf_agent.agent_loop import _existing_approval_granted_event

    case = _make_case(session, state=CaseState.DRAFT_READY)
    assert case.id is not None
    for kind in event_kinds:
        session.add(create_event(case_id=case.id, kind=kind, actor="human", detail={}))
    session.commit()

    agent_loop_says_pending = _existing_approval_granted_event(session, case.id) is not None
    case_actions_says_unconsumed = case_actions._has_unconsumed_approval_event(session, case.id)

    assert agent_loop_says_pending == case_actions_says_unconsumed


def test_attach_document_stores_it_available_in_the_vault(session: Session) -> None:
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None

    case_actions.attach_document(session, case.id, b"fake id document bytes", "image/png")

    documents = session.exec(select(Document).where(Document.case_id == case.id)).all()
    assert len(documents) == 1
    assert documents[0].status == DocumentStatus.AVAILABLE
    assert len(_events(session, case.id, "document_received")) == 1


def test_attach_document_also_selects_it_for_the_current_round(session: Session) -> None:
    """Issue #111: `attach_document()` keeps its pre-#111 external behavior (upload once, it's
    what the next resend uses) by immediately selecting the new upload, on top of storing it into
    the reusable vault."""
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None

    case_actions.attach_document(session, case.id, b"fake id document bytes", "image/png")

    document = session.exec(select(Document).where(Document.case_id == case.id)).one()
    selections = _events(session, case.id, "documents_selected_for_resend")
    assert len(selections) == 1
    assert selections[0].detail["document_ids"] == [document.id]


def test_attach_document_accepts_an_optional_label(session: Session) -> None:
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None
    case_actions.attach_document(session, case.id, b"bytes", "image/png", label="ID card")
    document = session.exec(select(Document).where(Document.case_id == case.id)).one()
    assert document.label == "ID card"


def test_attach_document_rejects_empty_content(session: Session) -> None:
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None
    with pytest.raises(InvalidInputError):
        case_actions.attach_document(session, case.id, b"", "image/png")


def test_attach_document_wrong_state(session: Session) -> None:
    """A genuinely non-editable state (neither Requires human action nor Draft ready) -- not
    Draft ready itself, which is now an editable round (LLM-drafted-replies extension), see
    `test_attach_document_allowed_while_draft_ready_and_not_yet_approved()` below."""
    case = _make_case(session, state=CaseState.AWAITING_RESPONSE)
    assert case.id is not None
    with pytest.raises(InvalidCaseStateError):
        case_actions.attach_document(session, case.id, b"bytes", "image/png")


def test_attach_document_allowed_while_draft_ready_and_not_yet_approved(session: Session) -> None:
    """LLM-drafted-replies extension: a human reviewing an already-composed draft can still attach
    a document before approving it -- the widened `_editable_round_case()` gate."""
    case = _make_case(session, state=CaseState.DRAFT_READY)
    assert case.id is not None
    _add_draft_event(session, case)

    case_actions.attach_document(session, case.id, b"bytes", "image/png")

    assert len(_events(session, case.id, "document_received")) == 1


def test_attach_document_refused_once_draft_ready_case_is_approved(session: Session) -> None:
    """R1: once approved, attaching a document must not silently apply to an already-authorized
    send -- mirrors `confirm_recipient()`'s identical "refuse once approved" precedent."""
    case = _make_case(session, state=CaseState.DRAFT_READY)
    assert case.id is not None
    _add_draft_event(session, case)
    _record_approval(session, case)

    with pytest.raises(InvalidCaseStateError):
        case_actions.attach_document(session, case.id, b"bytes", "image/png")


# --------------------------------------------------------------------------------------------- #
# select_documents_for_resend / list_vault_documents / delete_vault_document (issue #111)
# --------------------------------------------------------------------------------------------- #


def test_select_documents_for_resend_records_the_selection(session: Session) -> None:
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None
    case_actions.attach_document(session, case.id, b"doc a", "image/png")
    doc_a = session.exec(select(Document)).one()
    other_case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert other_case.id is not None
    case_actions.attach_document(session, other_case.id, b"doc b", "image/png")
    doc_b = session.exec(select(Document).where(Document.case_id == other_case.id)).one()

    case_actions.select_documents_for_resend(session, case.id, [doc_a.id, doc_b.id])

    selections = _events(session, case.id, "documents_selected_for_resend")
    latest = max(selections, key=lambda event: event.id or 0)
    assert sorted(latest.detail["document_ids"]) == sorted([doc_a.id, doc_b.id])


def test_select_documents_for_resend_allows_a_document_from_a_different_case(
    session: Session,
) -> None:
    """The whole point of the vault (issue #111): reuse across cases, not just within one."""
    origin_case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert origin_case.id is not None
    case_actions.attach_document(session, origin_case.id, b"id card", "image/png")
    document = session.exec(select(Document)).one()

    target_case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert target_case.id is not None

    result = case_actions.select_documents_for_resend(session, target_case.id, [document.id])
    assert result.id == target_case.id


def test_select_documents_for_resend_rejects_an_unknown_or_unavailable_id(
    session: Session,
) -> None:
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None
    with pytest.raises(InvalidInputError):
        case_actions.select_documents_for_resend(session, case.id, [999])


def test_select_documents_for_resend_rejects_a_purged_document(session: Session) -> None:
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None
    case_actions.attach_document(session, case.id, b"doc", "image/png")
    document = session.exec(select(Document)).one()
    case_actions.delete_vault_document(session, document.id)

    with pytest.raises(InvalidInputError):
        case_actions.select_documents_for_resend(session, case.id, [document.id])


def test_select_documents_for_resend_requires_at_least_one_id(session: Session) -> None:
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None
    with pytest.raises(InvalidInputError):
        case_actions.select_documents_for_resend(session, case.id, [])


def test_select_documents_for_resend_wrong_state(session: Session) -> None:
    """A genuinely non-editable state -- not Draft ready itself, now editable, see
    `test_select_documents_for_resend_allowed_while_draft_ready_and_not_yet_approved()` below."""
    case = _make_case(session, state=CaseState.AWAITING_RESPONSE)
    assert case.id is not None
    with pytest.raises(InvalidCaseStateError):
        case_actions.select_documents_for_resend(session, case.id, [1])


def test_select_documents_for_resend_allowed_while_draft_ready_and_not_yet_approved(
    session: Session,
) -> None:
    """LLM-drafted-replies extension: selecting an existing vault document also works while
    reviewing an already-composed, not-yet-approved draft."""
    origin_case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert origin_case.id is not None
    case_actions.attach_document(session, origin_case.id, b"id card", "image/png")
    document = session.exec(select(Document)).one()

    case = _make_case(session, state=CaseState.DRAFT_READY)
    assert case.id is not None
    _add_draft_event(session, case)

    result = case_actions.select_documents_for_resend(session, case.id, [document.id])

    assert result.id == case.id


def test_select_documents_for_resend_refused_once_draft_ready_case_is_approved(
    session: Session,
) -> None:
    case = _make_case(session, state=CaseState.DRAFT_READY)
    assert case.id is not None
    _add_draft_event(session, case)
    case_actions.attach_document(session, case.id, b"doc", "image/png")
    document = session.exec(select(Document)).one()
    _record_approval(session, case)

    with pytest.raises(InvalidCaseStateError):
        case_actions.select_documents_for_resend(session, case.id, [document.id])


def test_list_vault_documents_returns_only_available_ones_most_recent_first(
    session: Session,
) -> None:
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None
    case_actions.attach_document(session, case.id, b"first", "image/png")
    first = session.exec(select(Document)).one()
    case_actions.attach_document(session, case.id, b"second", "image/png")
    second = session.exec(select(Document).where(Document.id != first.id)).one()
    case_actions.attach_document(session, case.id, b"third", "image/png")
    third = session.exec(
        select(Document).where(col(Document.id).not_in([first.id, second.id]))
    ).one()
    case_actions.delete_vault_document(session, first.id)

    documents = case_actions.list_vault_documents(session)

    # Purged document excluded; the two remaining available ones ordered most-recently-uploaded
    # first -- a single-remaining-document assertion (the prior version of this test) can't
    # distinguish real ordering from an unordered query that happens to return one row.
    assert [document.id for document in documents] == [third.id, second.id]


def test_delete_vault_document_removes_it(session: Session) -> None:
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None
    case_actions.attach_document(session, case.id, b"doc", "image/png")
    document = session.exec(select(Document)).one()

    case_actions.delete_vault_document(session, document.id)

    session.refresh(document)
    assert document.status == DocumentStatus.PURGED


def test_delete_vault_document_rejects_an_unknown_id(session: Session) -> None:
    with pytest.raises(InvalidInputError):
        case_actions.delete_vault_document(session, 999)


def test_rename_vault_document_sets_the_label(session: Session) -> None:
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None
    case_actions.attach_document(session, case.id, b"doc", "image/png")
    document = session.exec(select(Document)).one()
    assert document.id is not None

    renamed = case_actions.rename_vault_document(session, document.id, "ID card front")

    assert renamed.label == "ID card front"
    session.refresh(document)
    assert document.label == "ID card front"


def test_rename_vault_document_records_an_event(session: Session) -> None:
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None
    case_actions.attach_document(session, case.id, b"doc", "image/png", label="old name")
    document = session.exec(select(Document)).one()
    assert document.id is not None

    case_actions.rename_vault_document(session, document.id, "new name")

    events = session.exec(
        select(Event).where(Event.case_id == document.case_id, Event.kind == "document_renamed")
    ).all()
    assert len(events) == 1
    assert events[0].detail == {"previous_label": "old name", "label": "new name"}


def test_rename_vault_document_strips_surrounding_whitespace(session: Session) -> None:
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None
    case_actions.attach_document(session, case.id, b"doc", "image/png")
    document = session.exec(select(Document)).one()
    assert document.id is not None

    renamed = case_actions.rename_vault_document(session, document.id, "  ID card front  ")

    assert renamed.label == "ID card front"


def test_rename_vault_document_rejects_a_blank_label(session: Session) -> None:
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None
    case_actions.attach_document(session, case.id, b"doc", "image/png")
    document = session.exec(select(Document)).one()
    assert document.id is not None

    with pytest.raises(InvalidInputError):
        case_actions.rename_vault_document(session, document.id, "   ")


def test_rename_vault_document_rejects_an_unknown_id(session: Session) -> None:
    with pytest.raises(InvalidInputError):
        case_actions.rename_vault_document(session, 999, "ID card")


def test_rename_vault_document_rejects_a_purged_document(session: Session) -> None:
    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    assert case.id is not None
    case_actions.attach_document(session, case.id, b"doc", "image/png")
    document = session.exec(select(Document)).one()
    assert document.id is not None
    case_actions.delete_vault_document(session, document.id)

    with pytest.raises(InvalidInputError):
        case_actions.rename_vault_document(session, document.id, "ID card")
