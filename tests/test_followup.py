"""Tests for the follow-up orchestration (`rtbf_agent.followup`) and its guarded `agent_loop`
transitions (issue #68).

Automated-case-loop rework, later narrowed by a maintainer-directed decision (see `followup.py`'s
module docstring, "Why CONFIRMATION auto-closes now"): a classified `REJECTION` reply still only
gets a one-way notice -- the actual terminal transition happens via `case_actions.
confirm_followup()`/`dispute_followup()` (kanban or CLI), tested directly in `tests/
test_case_actions.py`. A classified `CONFIRMATION` reply, however, now closes the case
immediately, itself, inside `follow_up()` -- no separate human call needed; still backed by a
persisted, audited `followup_confirmed` Event (actor `"agent"`), and still undoable via
`case_actions.reopen_case()` if the classification was wrong. R9 (no timeout/auto-escalation),
R3 (every classification audited), and the narrowed R1's-spirit guarantee (a terminal close is
always audited, not always pre-approved by a human for `CONFIRMATION`) are the restrictions under
test here.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from rtbf_agent.agent_loop import (
    apply_confirmed_followup_transition,
    flag_requires_human_action,
)
from rtbf_agent.core.interfaces import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    NotificationMessage,
    Notifier,
)
from rtbf_agent.core.models import Case, CaseState, Event, create_event
from rtbf_agent.followup import follow_up
from rtbf_agent.plugins.followup.detector import ResponseClassifier
from rtbf_agent.plugins.followup.monitor import InboxReplyMonitor, MailboxMessage

_SENT_AT = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


class _FakeMailboxReader:
    """Returns injected messages, ignoring the query -- Python-side matching is the real gate."""

    def __init__(self, messages: list[MailboxMessage]) -> None:
        self._messages = {message.message_id: message for message in messages}

    def list_message_ids(self, query: str) -> Sequence[str]:
        return list(self._messages)

    def get_message(self, message_id: str) -> MailboxMessage:
        return self._messages[message_id]


class _FakeLLMProvider(LLMProvider):
    def __init__(self, content: str) -> None:
        self._content = content

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(content=self._content, model=request.model)


class _FakeNotifier(Notifier):
    """Records every one-way notification -- `Notifier` is notify-only now (automated-case-loop
    rework; see `core.interfaces.Notifier`'s module docstring)."""

    def __init__(self) -> None:
        self.notifications: list[NotificationMessage] = []

    async def notify(self, message: NotificationMessage) -> None:
        self.notifications.append(message)


def _make_sent_case(
    session: Session,
    *,
    state: CaseState = CaseState.AWAITING_RESPONSE,
    sent_at: datetime = _SENT_AT,
) -> Case:
    """A `Case` that has genuinely been sent: monitored state + a `request_sent` audit event."""
    case = Case(
        site="Example Corp",
        domain="example.com",
        target_email="user@gmail.com",
        recipient_email="privacy@example-corp.com",
        state=state,
    )
    session.add(case)
    session.commit()
    session.refresh(case)
    assert case.id is not None
    session.add(
        create_event(case_id=case.id, kind="request_sent", actor="agent", timestamp=sent_at)
    )
    session.commit()
    return case


def _reply(
    *,
    message_id: str = "reply-1",
    from_address: str = "Support <support@example-corp.com>",
    minutes_after_send: int = 60,
    body: str = "We have deleted your account and all associated data.",
    subject: str = "Re: your deletion request",
    rfc822_message_id: str | None = None,
) -> MailboxMessage:
    return MailboxMessage(
        message_id=message_id,
        from_address=from_address,
        subject=subject,
        body=body,
        received_at=_SENT_AT + timedelta(minutes=minutes_after_send),
        rfc822_message_id=rfc822_message_id,
    )


def _monitor(messages: list[MailboxMessage]) -> InboxReplyMonitor:
    return InboxReplyMonitor(_FakeMailboxReader(messages))


def _classifier(intent: str, reasoning: str = "because the reply says so") -> ResponseClassifier:
    return ResponseClassifier(
        _FakeLLMProvider(json.dumps({"intent": intent, "reasoning": reasoning})), model="mock"
    )


def _events(session: Session, case_id: int, kind: str) -> list[Event]:
    return list(
        session.exec(select(Event).where(Event.case_id == case_id, Event.kind == kind)).all()
    )


def _commit_from_a_separate_session(session: Session, case_id: int, state: CaseState) -> None:
    """Commit `state` for `case_id` via a genuinely independent `Session` on the same engine --
    standing in for a concurrent `python -m rtbf_agent` invocation, not merely mutating the
    original Python object in place. Proves `session.refresh()` in the code under test picks up a
    truly-separately-committed change, not just a mocked return value."""
    with Session(session.get_bind()) as other_session:
        other_case = other_session.get(Case, case_id)
        assert other_case is not None
        other_case.state = state
        other_session.add(other_case)
        other_session.commit()


# --------------------------------------------------------------------------------------------- #
# Reply detection, classification, and (for confirmation/rejection) notify-only -- no auto-close.
# --------------------------------------------------------------------------------------------- #


async def test_reply_matching_a_sent_case_is_detected_and_classified(session: Session) -> None:
    case = _make_sent_case(session)
    monitor = _monitor([_reply()])
    classifier = _classifier("confirmation")
    notifier = _FakeNotifier()

    await follow_up(session, case, monitor, classifier, notifier)
    session.commit()

    classified = _events(session, case.id, "response_classified")
    assert len(classified) == 1
    assert classified[0].detail["message_id"] == "reply-1"
    assert classified[0].detail["intent"] == "confirmation"


async def test_confirmation_reply_auto_closes_the_case(session: Session) -> None:
    """Maintainer-directed narrowing of issue #68 (see `followup.py`'s module docstring, "Why
    CONFIRMATION auto-closes now"): a confirmation classification now closes the case immediately,
    itself -- no separate human `case_actions.confirm_followup()` call needed. A persisted,
    agent-authored `followup_confirmed` Event backs the transition, same as a human-authored one
    would."""
    case = _make_sent_case(session)
    notifier = _FakeNotifier()

    await follow_up(session, case, _monitor([_reply()]), _classifier("confirmation"), notifier)
    session.commit()

    assert case.state is CaseState.DELETION_CONFIRMED  # auto-closed, not merely proposed
    assert len(notifier.notifications) == 1
    classified = _events(session, case.id, "response_classified")
    assert len(classified) == 1
    # The intent field, not a substring of the notification text (OCR review finding): "confirm"
    # appears in both the CONFIRMATION and REJECTION notification texts ("confirm or dispute via
    # kanban/CLI"), so a substring check here wouldn't actually distinguish the two.
    assert classified[0].detail["intent"] == "confirmation"
    [confirmed_event] = _events(session, case.id, "followup_confirmed")
    assert confirmed_event.actor == "agent"
    assert confirmed_event.detail["target_state"] == CaseState.DELETION_CONFIRMED.value
    # The notification reflects a closure, not a request to confirm.
    notification_text = notifier.notifications[0].text
    assert "closed" in notification_text
    assert "reopen" in notification_text


async def test_rejection_reply_notifies_but_never_auto_transitions(session: Session) -> None:
    case = _make_sent_case(session)
    notifier = _FakeNotifier()
    classifier = _classifier("rejection", reasoning="They refuse to process the request.")

    await follow_up(session, case, _monitor([_reply()]), classifier, notifier)
    session.commit()

    assert case.state is CaseState.AWAITING_RESPONSE
    assert len(notifier.notifications) == 1
    classified = _events(session, case.id, "response_classified")
    assert classified[0].detail["intent"] == "rejection"


async def test_case_with_no_reply_yet_is_left_untouched_no_timeout_no_auto_escalation(
    session: Session,
) -> None:
    """R9: a monitoring pass over a case with no interpretable reply changes absolutely nothing --
    no state, no updated_at, no new Event, no notification. Proven byte-for-byte, not merely
    'no exception raised'."""
    case = _make_sent_case(session, state=CaseState.AWAITING_RESPONSE)
    # A plausible false-positive: the original signup mail from the same domain, arriving *before*
    # the send -- the pass must actively reject it and still change nothing.
    pre_send = _reply(
        message_id="signup", from_address="welcome@example-corp.com", minutes_after_send=-240
    )
    monitor = _monitor([pre_send])
    classifier = _classifier("confirmation")  # would fire if any reply were (wrongly) matched
    notifier = _FakeNotifier()

    state_before = case.state
    updated_at_before = case.updated_at
    events_before = session.exec(select(Event).where(Event.case_id == case.id)).all()
    count_before = len(events_before)

    result = await follow_up(session, case, monitor, classifier, notifier)
    session.commit()

    assert result is case
    assert case.state == state_before == CaseState.AWAITING_RESPONSE
    assert case.updated_at == updated_at_before
    assert len(session.exec(select(Event).where(Event.case_id == case.id)).all()) == count_before
    assert notifier.notifications == []


async def test_reply_classified_as_additional_info_request_moves_case_to_requires_human_action(
    session: Session,
) -> None:
    """The hand-off point to issue #69: no human confirmation gate (it's an attention flag, not a
    terminal close), just a transition + a notification."""
    case = _make_sent_case(session)
    notifier = _FakeNotifier()
    classifier = _classifier(
        "additional_info_request", reasoning="They ask for a photo ID to verify identity."
    )

    await follow_up(session, case, _monitor([_reply()]), classifier, notifier)
    session.commit()

    assert case.state is CaseState.REQUIRES_HUMAN_ACTION
    assert len(notifier.notifications) == 1
    assert len(_events(session, case.id, "additional_info_requested")) == 1


async def test_additional_info_requested_records_the_replys_own_message_id(
    session: Session,
) -> None:
    """Real report: without this, `agent_loop._thread_reference_ids()` has nothing to put in the
    eventual resend's `In-Reply-To`/`References` -- the resend looked like a fresh, unrelated email
    in the recipient's mail client instead of landing in the existing conversation."""
    case = _make_sent_case(session)
    notifier = _FakeNotifier()
    classifier = _classifier(
        "additional_info_request", reasoning="They ask for a photo ID to verify identity."
    )
    reply = _reply(rfc822_message_id="<reply-from-site@example-corp.com>")

    await follow_up(session, case, _monitor([reply]), classifier, notifier)
    session.commit()

    [event] = _events(session, case.id, "additional_info_requested")
    assert event.detail["reply_message_id_header"] == "<reply-from-site@example-corp.com>"


# --------------------------------------------------------------------------------------------- #
# Additional coverage: unclear replies, idempotence/no-double-resolution, guards.
# --------------------------------------------------------------------------------------------- #


async def test_unclear_reply_is_recorded_but_transitions_nothing(session: Session) -> None:
    case = _make_sent_case(session)
    notifier = _FakeNotifier()
    # LLM returns an out-of-vocabulary/unclear intent -> classify() yields None.
    classifier = _classifier("UNCLEAR")

    await follow_up(session, case, _monitor([_reply()]), classifier, notifier)
    session.commit()

    assert case.state is CaseState.AWAITING_RESPONSE  # unchanged
    assert notifier.notifications == []
    classified = _events(session, case.id, "response_classified")
    assert len(classified) == 1
    assert classified[0].detail["intent"] == "unclear"


async def test_same_reply_is_not_reclassified_on_a_second_pass(session: Session) -> None:
    """The message-id dedup: an unclear reply recorded on one pass is skipped on the next, so the
    same message never drives a second classification (nor a second LLM call)."""
    case = _make_sent_case(session)
    notifier = _FakeNotifier()
    monitor = _monitor([_reply()])

    await follow_up(session, case, monitor, _classifier("UNCLEAR"), notifier)
    session.commit()
    # Second pass with a classifier that WOULD confirm -- but the reply is already processed.
    await follow_up(session, case, monitor, _classifier("confirmation"), notifier)
    session.commit()

    assert case.state is CaseState.AWAITING_RESPONSE
    assert len(_events(session, case.id, "response_classified")) == 1


async def test_reply_processed_by_one_case_is_not_reclassified_by_another_sharing_its_domain(
    session: Session,
) -> None:
    """Two `Case`s can share a recipient domain (e.g. a company routing several brands'/regions'
    deletion replies through one centralized privacy contact) -- `InboxReplyMonitor` matches on
    sender domain, not a thread/`In-Reply-To` header, so both would otherwise see the exact same
    incoming message as a candidate reply. The processed-id dedup is deliberately global (not
    per-case, see `followup._processed_reply_message_ids()`) so only the first case's pass ever
    classifies a given message; the second case's pass must find it already processed and leave
    that case completely untouched -- never independently proposing (or auto-acting on) a
    transition for a reply that was never actually about it."""
    case_a = _make_sent_case(session)
    case_b = _make_sent_case(session)
    assert case_a.id != case_b.id
    reply = _reply()  # same recipient domain (example-corp.com) as both cases
    monitor = _monitor([reply])
    notifier = _FakeNotifier()

    await follow_up(session, case_a, monitor, _classifier("confirmation"), notifier)
    session.commit()
    await follow_up(session, case_b, monitor, _classifier("confirmation"), notifier)
    session.commit()

    # Only case_a (the first pass to reach the message) claimed it.
    assert len(_events(session, case_a.id, "response_classified")) == 1

    # case_b never classified the same message and is left exactly as it was (R9: no wrong
    # auto-action from a reply that wasn't about it).
    assert case_b.state == CaseState.AWAITING_RESPONSE
    assert _events(session, case_b.id, "response_classified") == []
    assert len(notifier.notifications) == 1  # only case_a's classification was ever notified


async def test_confirmed_terminal_case_is_untouched_by_a_later_pass(session: Session) -> None:
    """Double-resolution guard: once a case is auto-closed terminal by `follow_up()` itself (a
    CONFIRMATION classification, per the maintainer-directed narrowing of issue #68), it's out of
    the monitored set, so a later `follow_up()` pass (even with a fresh reply) is a no-op -- the
    same shape that stops a double send."""
    case = _make_sent_case(session)
    notifier = _FakeNotifier()
    await follow_up(session, case, _monitor([_reply()]), _classifier("confirmation"), notifier)
    session.commit()
    assert case.state is CaseState.DELETION_CONFIRMED
    [confirmed_event] = _events(session, case.id, "followup_confirmed")
    assert confirmed_event.actor == "agent"
    assert confirmed_event.detail["target_state"] == CaseState.DELETION_CONFIRMED.value

    later = _FakeNotifier()
    fresh_reply = _reply(message_id="reply-2", minutes_after_send=120)
    await follow_up(session, case, _monitor([fresh_reply]), _classifier("rejection"), later)
    session.commit()

    assert case.state is CaseState.DELETION_CONFIRMED  # unchanged
    assert later.notifications == []
    assert len(_events(session, case.id, "response_classified")) == 1  # only the first pass's


async def test_follow_up_is_a_noop_for_a_case_not_in_a_monitored_state(session: Session) -> None:
    case = _make_sent_case(session, state=CaseState.AWAITING_RESPONSE)
    case.state = CaseState.DRAFT_READY  # not a monitored state
    session.add(case)
    session.commit()
    notifier = _FakeNotifier()

    await follow_up(session, case, _monitor([_reply()]), _classifier("confirmation"), notifier)

    assert case.state is CaseState.DRAFT_READY
    assert notifier.notifications == []


async def test_follow_up_is_a_noop_when_no_request_sent_event_exists(session: Session) -> None:
    """A case somehow in a monitored state with no `request_sent` record has no send baseline to
    compare replies against -- it is left untouched rather than inventing one."""
    case = Case(
        site="Example Corp",
        domain="example.com",
        target_email="user@gmail.com",
        recipient_email="privacy@example-corp.com",
        state=CaseState.AWAITING_RESPONSE,
    )
    session.add(case)
    session.commit()
    notifier = _FakeNotifier()

    await follow_up(session, case, _monitor([_reply()]), _classifier("confirmation"), notifier)

    assert case.state is CaseState.AWAITING_RESPONSE
    assert notifier.notifications == []


async def test_flag_requires_human_action_refreshes_case_before_checking_state(
    session: Session,
) -> None:
    """`flag_requires_human_action()` itself awaits nothing before its state check, but its caller
    (`followup.follow_up()`) already awaited real I/O (the reply monitor, then the classifier)
    earlier in the same pass -- during which a concurrent invocation could have already resolved
    this exact case. Simulated with a genuinely separate `Session` committing that resolution
    before this call even starts, standing in for another process having already closed the case
    -- proving the function's own `session.refresh(case)` picks up a truly-committed change."""
    case = _make_sent_case(session)
    assert case.id is not None
    notifier = _FakeNotifier()

    _commit_from_a_separate_session(session, case.id, CaseState.DELETION_CONFIRMED)

    with pytest.raises(ValueError, match="requires case.state"):
        await flag_requires_human_action(
            session, case, notifier, kind="additional_info_requested", summary="?"
        )

    # The concurrent resolution (Deletion confirmed) is what stands -- never overwritten with
    # Requires human action by this call's stale view of the case.
    session.refresh(case)
    assert case.state == CaseState.DELETION_CONFIRMED


async def test_apply_confirmed_transition_rejects_a_wrong_kind_event(session: Session) -> None:
    """The guard refuses an event of the wrong kind, even if persisted for this case -- only a
    genuine `followup_confirmed` event may drive a terminal transition."""
    case = _make_sent_case(session)
    assert case.id is not None
    wrong = create_event(case_id=case.id, kind="request_sent", actor="agent")
    session.add(wrong)
    session.flush()
    with pytest.raises(RuntimeError, match="followup_confirmed"):
        apply_confirmed_followup_transition(session, case, wrong, CaseState.DELETION_CONFIRMED)


async def test_apply_confirmed_transition_rejects_an_unflushed_event(session: Session) -> None:
    case = _make_sent_case(session)
    assert case.id is not None
    unflushed = create_event(case_id=case.id, kind="followup_confirmed", actor="human")
    # Never added/flushed -> no id -> guard must refuse.
    with pytest.raises(RuntimeError, match="followup_confirmed"):
        apply_confirmed_followup_transition(session, case, unflushed, CaseState.DELETION_CONFIRMED)
