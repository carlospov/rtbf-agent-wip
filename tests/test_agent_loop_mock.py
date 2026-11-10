"""Tests for `rtbf_agent.agent_loop` — the mocked end-to-end orchestrator (issue #10).

Covers the restrictions this module discharges (`docs/planning/RESTRICTIONS.md`):
R1 (send/attach never executes without a send-time approval `Event`), R3 (proposal events
recorded even before an approval decision is known), R9 (no code path auto-transitions a `Case`
out of a waiting state).
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from rtbf_agent.agent_loop import (
    _classify,
    _draft,
    _execute_send,
    compose_additional_info_draft,
    run_cycle,
    send_request,
)
from rtbf_agent.config import GlobalConfig
from rtbf_agent.core.interfaces import EmailMessage, Sender
from rtbf_agent.core.models import Case, CaseState, Event, create_event
from rtbf_agent.plugins.legal_templates.jurisdictions.gdpr import LEGAL_BASIS as GDPR_LEGAL_BASIS
from rtbf_agent.plugins.legal_templates.provider import RegistryLegalTemplateProvider
from rtbf_agent.plugins.llm.mock import MockLLMProvider
from rtbf_agent.plugins.notifiers.console import ConsoleNotifier

_TEST_MODEL = "test-model"
"""Stand-in for a real, resolved LLM model name (issue #86 follow-up) -- `MockLLMProvider` ignores
`request.model` entirely, so any string works here; this just documents that every `run_cycle()`/
`_classify()` call site must supply one explicitly (no default -- see `agent_loop._classify()`'s
docstring for why)."""


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture()
def legal_provider() -> RegistryLegalTemplateProvider:
    return RegistryLegalTemplateProvider()


@pytest.fixture()
def global_config() -> GlobalConfig:
    """A fully-resolvable global config: GDPR/English defaults plus a requester name.

    Individual tests that need to exercise the "nothing resolvable" error path build their own
    `GlobalConfig(None, None, None)` instead of using this fixture.
    """
    return GlobalConfig(
        requester_name="Ada Lovelace",
        default_applicable_jurisdiction="GDPR",
        default_drafting_language="en",
    )


def _make_case(session: Session, **overrides) -> Case:
    defaults = {
        "site": "Example Site",
        "domain": "example.com",
        "target_email": "user@example.com",
        # The site's real deletion-request recipient (issue #52) -- deliberately different from
        # `target_email` (the user's own scanned mailbox) so tests that reach a real send can prove
        # the outbound `to_address` is this address, never the user's own inbox. Tests exercising
        # the "no recipient configured" guard override this back to `None`.
        "recipient_email": "privacy@example-corp.com",
    }
    defaults.update(overrides)
    case = Case(**defaults)
    session.add(case)
    session.commit()
    session.refresh(case)
    return case


def _add_draft_event(
    session: Session,
    case: Case,
    draft: str = "Please delete my data.",
    subject: str = "Deletion request",
) -> None:
    """Real cases always reach `Draft ready` via `_draft()`, which writes a `draft_proposed`
    `Event` -- tests that construct a case directly in `Draft ready` need to add one manually
    so `_execute_send()`'s `latest_draft()` lookup has something to find."""
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


class _SpySender(Sender):
    """Records every `send()` call; a test fails if this is ever invoked without a valid guard."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


class _FailingSender(Sender):
    """Always raises -- simulates a transport-level dispatch failure (auth error, network, ...)."""

    async def send(self, message: EmailMessage) -> None:
        raise RuntimeError("simulated SMTP failure")


class _FakeIO:
    def __init__(self) -> None:
        self.printed: list[str] = []

    def print_fn(self, text: str) -> None:
        self.printed.append(text)


def _record_approval(session: Session, case: Case) -> Event:
    """Record an `approval_granted` `Event` directly -- the test-side stand-in for
    `case_actions.approve_draft()` (kanban/CLI), since `send_request()` no longer solicits one
    itself (automated-case-loop rework: see `core.interfaces.Notifier`'s module docstring)."""
    assert case.id is not None
    event = create_event(
        case_id=case.id, kind="approval_granted", actor="human", detail={"action": "send_request"}
    )
    session.add(event)
    session.commit()
    return event


def _events_for(session: Session, case: Case) -> list[Event]:
    return list(session.exec(select(Event).where(Event.case_id == case.id).order_by(Event.id)))


# --- run_cycle: discover(implicit) -> classify -> draft -> notify --------------------------


async def test_run_cycle_ends_in_borrador_listo_and_never_auto_advances_past_it(
    session: Session, legal_provider: RegistryLegalTemplateProvider, global_config: GlobalConfig
) -> None:
    case = _make_case(session)
    llm = MockLLMProvider()
    io = _FakeIO()
    notifier = ConsoleNotifier(print_fn=io.print_fn)

    result = await run_cycle(
        session, case, llm, notifier, legal_provider, global_config, model=_TEST_MODEL
    )
    session.commit()

    assert result.state == CaseState.DRAFT_READY
    kinds = [e.kind for e in _events_for(session, case)]
    assert kinds == ["classification_proposed", "draft_proposed"]
    # The draft-ready notice went out (informational only -- no decision solicited).
    assert any("example.com" in line for line in io.printed)


async def test_run_cycle_is_a_no_op_once_borrador_listo(
    session: Session, legal_provider: RegistryLegalTemplateProvider, global_config: GlobalConfig
) -> None:
    case = _make_case(session)
    llm = MockLLMProvider()
    io = _FakeIO()
    notifier = ConsoleNotifier(print_fn=io.print_fn)

    await run_cycle(session, case, llm, notifier, legal_provider, global_config, model=_TEST_MODEL)
    session.commit()
    events_after_first_cycle = _events_for(session, case)
    printed_after_first_cycle = list(io.printed)

    # Calling it again must not add another event, re-notify, or change state (R9).
    result = await run_cycle(
        session, case, llm, notifier, legal_provider, global_config, model=_TEST_MODEL
    )
    session.commit()

    assert result.state == CaseState.DRAFT_READY
    assert _events_for(session, case) == events_after_first_cycle
    assert io.printed == printed_after_first_cycle


@pytest.mark.parametrize(
    "waiting_state",
    [
        CaseState.AWAITING_HUMAN_APPROVAL,
        CaseState.AWAITING_RESPONSE,
        CaseState.REQUIRES_HUMAN_ACTION,
    ],
)
async def test_case_in_waiting_state_never_auto_transitions_without_event(
    session: Session,
    waiting_state: CaseState,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """`run_cycle` must never advance (or otherwise touch) a `Case` already sitting in a state it
    isn't responsible for -- proving there's no implicit "keep going" path past a human gate."""
    case = _make_case(session, state=waiting_state)
    llm = MockLLMProvider()
    io = _FakeIO()
    notifier = ConsoleNotifier(print_fn=io.print_fn)

    result = await run_cycle(
        session, case, llm, notifier, legal_provider, global_config, model=_TEST_MODEL
    )
    session.commit()

    assert result.state == waiting_state
    assert _events_for(session, case) == []
    assert io.printed == []


# --- send_request / _execute_send: the only approval-gated transition ----------------------
#
# send_request() no longer solicits a decision itself (automated-case-loop rework -- see
# core.interfaces.Notifier's module docstring): it only checks for an already-recorded
# `approval_granted` Event (case_actions.approve_draft(), kanban/CLI) and sends if found,
# otherwise leaves the case untouched. `_record_approval()` above is the test-side stand-in for
# that external recording step.


async def test_send_request_requires_borrador_listo_state(session: Session) -> None:
    case = _make_case(session, state=CaseState.DISCOVERED)
    sender = _SpySender()

    with pytest.raises(ValueError, match="Draft ready"):
        await send_request(session, case, sender)
    assert sender.sent == []


async def test_send_request_sends_once_a_decision_is_already_recorded(session: Session) -> None:
    case = _make_case(session, state=CaseState.DRAFT_READY)
    _add_draft_event(session, case, draft="Please delete my data.")
    _record_approval(session, case)
    sender = _SpySender()

    result = await send_request(session, case, sender)
    session.commit()

    assert result.state == CaseState.AWAITING_RESPONSE
    kinds = [e.kind for e in _events_for(session, case)]
    # Approval is recorded strictly before the send it authorizes (R1).
    assert kinds == ["draft_proposed", "approval_granted", "request_sent"]
    # The actual dispatch happened, carrying the drafted body and the site's real recipient
    # address (issue #52) -- never the user's own scanned mailbox (`target_email`).
    assert len(sender.sent) == 1
    assert sender.sent[0].to_address == case.recipient_email
    assert sender.sent[0].to_address != case.target_email
    assert sender.sent[0].body == "Please delete my data."
    approval_events = [e for e in _events_for(session, case) if e.kind == "approval_granted"]
    assert len(approval_events) == 1  # not duplicated
    # The `Message-ID` generated for the outbound send is both handed to the `Sender` (so a real
    # one can put it on the wire) and recorded on `request_sent`, so a later follow-up pass can
    # search for the reply by `In-Reply-To`/`References` instead of a broad sender-domain guess.
    request_sent_event = next(e for e in _events_for(session, case) if e.kind == "request_sent")
    sent_message_id = request_sent_event.detail["sent_message_id"]
    assert isinstance(sent_message_id, str) and sent_message_id
    assert sender.sent[0].message_id == sent_message_id
    # A case's very first send has nothing to thread yet -- sent as a fresh message, not a reply.
    assert sender.sent[0].in_reply_to is None
    assert sender.sent[0].references is None
    assert not sender.sent[0].subject.lower().startswith("re:")


async def test_resend_threads_as_a_reply_to_the_sites_own_message(session: Session) -> None:
    """Real report: every email this tool sent generated a brand-new `Message-ID` with no
    `In-Reply-To`/`References` at all -- a resend after "we need your ID" looked like a fresh,
    unrelated email in the recipient's mail client instead of landing in the existing
    conversation."""
    case = _make_case(session, state=CaseState.DRAFT_READY)
    _add_draft_event(session, case, draft="Please delete my data.", subject="Deletion request")
    _record_approval(session, case)
    sender = _SpySender()
    await send_request(session, case, sender)
    session.commit()
    first_sent_message_id = sender.sent[0].message_id

    session.add(
        create_event(
            case_id=case.id,
            kind="additional_info_requested",
            actor="agent",
            detail={
                "message_id": "reply-1",
                "reasoning": "asked for a photo ID",
                "reply_message_id_header": "<reply-from-site@example-corp.com>",
            },
        )
    )
    case.state = CaseState.REQUIRES_HUMAN_ACTION
    session.add(case)
    session.commit()

    # compose_additional_info_draft() only previews the reply (case.state stays Requires human
    # action) -- the drafting-is-a-preview redesign that superseded issue #117/#119's fused
    # draft+send. A separate, explicit approval + send_request() call moves it on, exactly like any
    # other draft.
    result = await compose_additional_info_draft(
        session,
        case,
        MockLLMProvider(),
        _TEST_MODEL,
        extra_text="my date of birth is 1990-01-01",
    )
    session.commit()
    assert result.state == CaseState.REQUIRES_HUMAN_ACTION

    case.state = CaseState.DRAFT_READY
    session.add(case)
    _record_approval(session, case)
    result = await send_request(session, case, sender)
    session.commit()

    assert result.state == CaseState.AWAITING_RESPONSE
    assert len(sender.sent) == 2
    resend = sender.sent[1]
    assert resend.in_reply_to == "<reply-from-site@example-corp.com>"
    assert resend.references == f"{first_sent_message_id} <reply-from-site@example-corp.com>"
    assert resend.subject == "Re: Deletion request"

    # A second round: the site asks for something else, chaining onto everything so far.
    second_sent_message_id = resend.message_id
    session.add(
        create_event(
            case_id=case.id,
            kind="additional_info_requested",
            actor="agent",
            detail={
                "message_id": "reply-2",
                "reasoning": "asked for a second document",
                "reply_message_id_header": "<second-reply-from-site@example-corp.com>",
            },
        )
    )
    case.state = CaseState.REQUIRES_HUMAN_ACTION
    session.add(case)
    session.commit()

    await compose_additional_info_draft(
        session,
        case,
        MockLLMProvider(),
        _TEST_MODEL,
        extra_text="here is the second document's context",
    )
    session.commit()
    case.state = CaseState.DRAFT_READY
    session.add(case)
    _record_approval(session, case)
    await send_request(session, case, sender)
    session.commit()

    assert len(sender.sent) == 3
    second_resend = sender.sent[2]
    assert second_resend.in_reply_to == "<second-reply-from-site@example-corp.com>"
    assert second_resend.references == (
        f"{first_sent_message_id} <reply-from-site@example-corp.com> "
        f"{second_sent_message_id} <second-reply-from-site@example-corp.com>"
    )


async def test_compose_additional_info_draft_refuses_if_approved_concurrently(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Code-reviewer finding on this PR (check-then-act race, R1, same shape as `case_actions.
    edit_draft()`/`select_documents_for_resend()`): if a concurrent `case_actions.approve_draft()`
    call commits in the gap between this function's initial `case.state` check and its own
    `draft_proposed` Event write, this redraft must not be left on record as the "latest" body
    under someone else's approval -- it must roll back and refuse instead. Simulated the same way
    this repo's other race tests do: `monkeypatch` makes the post-flush recheck,
    `_existing_approval_granted_event()`, answer "an approval exists" -- standing in for a real
    concurrent commit landing in that gap, without needing two actual threads/connections."""
    import rtbf_agent.agent_loop as agent_loop_module

    case = _make_case(session, state=CaseState.REQUIRES_HUMAN_ACTION)
    _add_draft_event(session, case, draft="original draft", subject="Deletion request")
    session.add(
        create_event(case_id=case.id, kind="additional_info_requested", actor="agent", detail={})
    )
    session.commit()

    call_count = 0

    def _racing_check(session_arg: Session, case_id_arg: int) -> Event | None:
        nonlocal call_count
        call_count += 1
        return create_event(
            case_id=case_id_arg,
            kind="approval_granted",
            actor="human",
            detail={"action": "send_request"},
        )

    monkeypatch.setattr(agent_loop_module, "_existing_approval_granted_event", _racing_check)

    with pytest.raises(ValueError, match="approved concurrently"):
        await compose_additional_info_draft(
            session, case, MockLLMProvider(), _TEST_MODEL, extra_text="a redraft nobody approved"
        )

    assert call_count == 1  # the re-check actually ran
    draft_events = list(
        session.exec(
            select(Event).where(Event.case_id == case.id, Event.kind == "draft_proposed")
        ).all()
    )
    assert len(draft_events) == 1  # only the original -- the racing redraft was rolled back
    assert draft_events[0].detail["draft"] == "original draft"


async def test_send_request_is_a_noop_when_no_decision_is_recorded_yet(session: Session) -> None:
    """R9: a case with no recorded decision yet is left completely untouched -- no blocking wait,
    no exception, just a no-op this pass; a later pass (once a decision is recorded) will send
    it."""
    case = _make_case(session, state=CaseState.DRAFT_READY)
    _add_draft_event(session, case, draft="Please delete my data.")
    sender = _SpySender()

    result = await send_request(session, case, sender)
    session.commit()

    assert result.state == CaseState.DRAFT_READY
    assert sender.sent == []
    kinds = [e.kind for e in _events_for(session, case)]
    assert kinds == ["draft_proposed"]


async def test_send_without_approval_event_raises(session: Session) -> None:
    """`_execute_send` -- the only code path that flips `state` to `Awaiting response` -- refuses
    to run without an already-persisted `approval_granted` `Event` for this exact case (R1). This
    proves the guarantee holds even against a caller that tries to invoke it directly, bypassing
    `send_request()` entirely."""
    case = _make_case(session, state=CaseState.DRAFT_READY)
    sender = _SpySender()

    with pytest.raises(RuntimeError, match="approval_granted"):
        await _execute_send(session, case, None, sender)

    assert case.state == CaseState.DRAFT_READY
    assert sender.sent == []


async def test_execute_send_uses_case_recipient_email_not_target_email(session: Session) -> None:
    """The outbound `EmailMessage.to_address` must be the site's real recipient
    (`case.recipient_email`), never the user's own scanned mailbox (`case.target_email`). This is
    the core fix for issue #52: a real send goes to the company whose data is being deleted, not
    back to the user who asked. Exercised against a real (non-mocked-away) `_execute_send()` call
    with a valid, persisted approval `Event`."""
    case = _make_case(
        session,
        state=CaseState.DRAFT_READY,
        target_email="me@personal.example",
        recipient_email="dpo@some-company.example",
    )
    _add_draft_event(session, case)
    approval_event = create_event(
        case_id=case.id, kind="approval_granted", actor="human", detail={}
    )
    session.add(approval_event)
    session.flush()
    sender = _SpySender()

    await _execute_send(session, case, approval_event, sender)

    assert len(sender.sent) == 1
    assert sender.sent[0].to_address == "dpo@some-company.example"
    assert sender.sent[0].to_address != case.target_email


async def test_send_request_raises_if_no_recipient_email_configured(session: Session) -> None:
    """A `Case` reaching `Draft ready` with no `recipient_email` set must make `send_request()`
    fail loud (issue #52, R1) -- it must never silently fall back to `case.target_email` (emailing
    the user their own request) or send to `None`. Fires even with a decision already recorded,
    since a recipient is a separate, earlier decision (`case_actions.confirm_recipient()`)."""
    case = _make_case(session, state=CaseState.DRAFT_READY, recipient_email=None)
    _add_draft_event(session, case)
    _record_approval(session, case)
    sender = _SpySender()

    with pytest.raises(ValueError, match="recipient_email"):
        await send_request(session, case, sender)

    assert case.state == CaseState.DRAFT_READY
    assert sender.sent == []


async def test_send_request_solicits_fresh_approval_for_a_resent_case_despite_an_earlier_one(
    session: Session,
) -> None:
    """A `Case` that cycles back through `Draft ready` a second time (issue #69's resend loop --
    a reply asks for more info, then a fresh draft is resent) must require its own fresh
    `approval_granted` Event, not reuse the *first* round's already-consumed one just because it
    still exists in the case's history. Regression test for a real bug: an early version of
    `_existing_approval_granted_event()` looked for any `approval_granted` Event for the case at
    all, regardless of whether an intervening `request_sent` had already consumed it."""
    case = _make_case(session, state=CaseState.DRAFT_READY)
    _add_draft_event(session, case, draft="Round one draft.")
    assert case.id is not None

    _record_approval(session, case)
    first_sender = _SpySender()
    await send_request(session, case, first_sender)
    session.commit()
    assert case.state == CaseState.AWAITING_RESPONSE
    assert len(first_sender.sent) == 1

    # The case cycles back to Draft ready for a second round (simulating an additional-info
    # draft+approve without needing the full followup machinery here) -- no fresh approval
    # recorded yet.
    case.state = CaseState.DRAFT_READY
    session.add(case)
    session.add(
        create_event(
            case_id=case.id,
            kind="draft_proposed",
            actor="agent",
            detail={"draft": "Round two draft.", "subject": "Deletion request"},
        )
    )
    session.commit()

    second_sender = _SpySender()
    result = await send_request(session, case, second_sender)
    session.commit()

    # No decision recorded for this second round yet -- must not reuse round one's (already
    # consumed by its own request_sent) approval_granted Event.
    assert result.state == CaseState.DRAFT_READY
    assert second_sender.sent == []

    _record_approval(session, case)
    result = await send_request(session, case, second_sender)
    session.commit()

    assert result.state == CaseState.AWAITING_RESPONSE
    assert len(second_sender.sent) == 1
    assert second_sender.sent[0].body == "Round two draft."
    approval_events = [e for e in _events_for(session, case) if e.kind == "approval_granted"]
    assert len(approval_events) == 2  # one per round, not reused
    sent_events = [e for e in _events_for(session, case) if e.kind == "request_sent"]
    assert len(sent_events) == 2


async def test_execute_send_raises_and_does_not_send_if_no_recipient_email(
    session: Session,
) -> None:
    """`_execute_send()` -- the authoritative, un-bypassable gate (like the approval-event guard) --
    also refuses to reach a `Sender` when `recipient_email` is unset, even handed a fully valid,
    persisted approval `Event`. Proves the no-fallback-to-target_email guarantee holds against a
    caller that bypasses `send_request()` entirely (issue #52, R1)."""
    case = _make_case(session, state=CaseState.DRAFT_READY, recipient_email=None)
    _add_draft_event(session, case)
    approval_event = create_event(
        case_id=case.id, kind="approval_granted", actor="human", detail={}
    )
    session.add(approval_event)
    session.flush()
    sender = _SpySender()

    with pytest.raises(RuntimeError, match="recipient_email"):
        await _execute_send(session, case, approval_event, sender)

    assert case.state == CaseState.DRAFT_READY
    assert sender.sent == []
    assert "request_sent" not in [e.kind for e in _events_for(session, case)]


async def test_send_without_approval_event_raises_on_wrong_kind(session: Session) -> None:
    case = _make_case(session, state=CaseState.DRAFT_READY)
    wrong_event = create_event(case_id=case.id, kind="draft_proposed", actor="agent", detail={})
    sender = _SpySender()

    with pytest.raises(RuntimeError, match="approval_granted"):
        await _execute_send(session, case, wrong_event, sender)

    assert sender.sent == []


async def test_send_without_approval_event_raises_on_wrong_case(session: Session) -> None:
    case = _make_case(session, state=CaseState.DRAFT_READY)
    other_case = _make_case(session, domain="other.example")
    someone_elses_approval = create_event(
        case_id=other_case.id, kind="approval_granted", actor="human", detail={}
    )
    sender = _SpySender()

    with pytest.raises(RuntimeError, match="approval_granted"):
        await _execute_send(session, case, someone_elses_approval, sender)

    assert sender.sent == []


async def test_send_without_approval_event_raises_if_event_never_persisted(
    session: Session,
) -> None:
    """An `Event` built via `create_event()` but never `session.add()`-ed/flushed has `id is None`
    -- `_execute_send` must reject it even though every field value (`kind`, `case_id`) looks
    correct, since the whole point of the guard is proof the approval was actually recorded, not
    just that some in-memory object claims it was (R1)."""
    case = _make_case(session, state=CaseState.DRAFT_READY)
    unpersisted_approval = create_event(
        case_id=case.id, kind="approval_granted", actor="human", detail={}
    )
    assert unpersisted_approval.id is None
    sender = _SpySender()

    with pytest.raises(RuntimeError, match="approval_granted"):
        await _execute_send(session, case, unpersisted_approval, sender)

    assert sender.sent == []
    assert case.state == CaseState.DRAFT_READY


async def test_execute_send_calls_sender_only_after_approval_event_guard_passes(
    session: Session,
) -> None:
    """Proves the guard-then-send ordering directly: a `Sender` spy that would fail this test if
    invoked without a valid `approval_granted` `Event` is never reached by any of the three
    guard-rejecting calls, and is reached exactly once by the one call with a valid approval
    `Event` for the exact case."""
    case = _make_case(session, state=CaseState.DRAFT_READY)
    _add_draft_event(session, case)
    other_case = _make_case(session, domain="other.example")
    sender = _SpySender()

    with pytest.raises(RuntimeError, match="approval_granted"):
        await _execute_send(session, case, None, sender)
    with pytest.raises(RuntimeError, match="approval_granted"):
        await _execute_send(
            session,
            case,
            create_event(case_id=case.id, kind="draft_proposed", actor="agent", detail={}),
            sender,
        )
    with pytest.raises(RuntimeError, match="approval_granted"):
        await _execute_send(
            session,
            case,
            create_event(case_id=other_case.id, kind="approval_granted", actor="human", detail={}),
            sender,
        )
    assert sender.sent == []  # not one of the three rejected guard calls reached the sender

    valid_approval = create_event(
        case_id=case.id, kind="approval_granted", actor="human", detail={}
    )
    session.add(valid_approval)
    session.flush()
    await _execute_send(session, case, valid_approval, sender)

    assert len(sender.sent) == 1
    assert case.state == CaseState.AWAITING_RESPONSE


async def test_execute_send_uses_subject_from_draft_event_not_a_hardcoded_literal(
    session: Session,
) -> None:
    """The `EmailMessage.subject` handed to `Sender.send()` must come from the `draft_proposed`
    `Event` -- the same source as the body -- never invented independently in `_execute_send()`.
    Otherwise a `Case` drafted in a different language/jurisdiction (once issue #34 wires
    `_draft()` to `LegalTemplateProvider`) would still get an English subject unrelated to what was
    actually drafted."""
    case = _make_case(session, state=CaseState.DRAFT_READY)
    _add_draft_event(
        session,
        case,
        draft="Corps, veuillez supprimer mes données.",
        subject="Demande de suppression",
    )
    approval_event = create_event(
        case_id=case.id, kind="approval_granted", actor="human", detail={}
    )
    session.add(approval_event)
    session.flush()
    sender = _SpySender()

    await _execute_send(session, case, approval_event, sender)

    assert len(sender.sent) == 1
    assert sender.sent[0].subject == "Demande de suppression"
    assert sender.sent[0].body == "Corps, veuillez supprimer mes données."


async def test_execute_send_does_not_transition_case_or_log_event_if_send_fails(
    session: Session,
) -> None:
    """A `Sender` that raises (simulating a real transport failure) must leave `case.state`
    unchanged and produce no `request_sent` `Event` -- a failed dispatch must never look like a
    successful one in the audit trail (R1, R4)."""
    case = _make_case(session, state=CaseState.DRAFT_READY)
    _add_draft_event(session, case)
    approval_event = create_event(
        case_id=case.id, kind="approval_granted", actor="human", detail={}
    )
    session.add(approval_event)
    session.flush()

    with pytest.raises(RuntimeError, match="simulated SMTP failure"):
        await _execute_send(session, case, approval_event, _FailingSender())

    assert case.state == CaseState.DRAFT_READY
    kinds = [e.kind for e in _events_for(session, case)]
    assert "request_sent" not in kinds


async def test_full_mocked_cycle_end_to_end(
    session: Session, legal_provider: RegistryLegalTemplateProvider, global_config: GlobalConfig
) -> None:
    """The end-to-end proof spec S9 asks for: one `Case`, discover(implicit)->classify->draft->
    notify->[human approves]->send, using only `MockLLMProvider` and `ConsoleNotifier` -- no real
    connector anywhere in the path."""
    case = _make_case(session)
    llm = MockLLMProvider()
    io = _FakeIO()
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_cycle(session, case, llm, notifier, legal_provider, global_config, model=_TEST_MODEL)
    session.commit()
    assert case.state == CaseState.DRAFT_READY

    _record_approval(session, case)
    await send_request(session, case, sender)
    session.commit()

    assert case.state == CaseState.AWAITING_RESPONSE
    kinds = [e.kind for e in _events_for(session, case)]
    assert kinds == [
        "classification_proposed",
        "draft_proposed",
        "approval_granted",
        "request_sent",
    ]
    assert len(sender.sent) == 1


# --- _draft(): real LegalTemplateProvider content, not freeform LLM text (issue #34) --------


async def test_draft_uses_legal_template_provider_not_freeform_llm_content(
    session: Session, legal_provider: RegistryLegalTemplateProvider, global_config: GlobalConfig
) -> None:
    """The `draft_proposed` `Event.detail` must contain the jurisdiction's exact `legal_basis`
    text -- proving `_draft()` pulls the body from `LegalTemplateProvider`, not an LLM's
    paraphrase of it."""
    case = _make_case(
        session,
        applicable_jurisdiction="GDPR",
        drafting_language="en",
        state=CaseState.CLASSIFIED,
    )

    await _draft(session, case, legal_provider, global_config)
    session.commit()

    assert case.state == CaseState.DRAFT_READY
    [event] = _events_for(session, case)
    assert event.kind == "draft_proposed"
    # The exact GDPR legal-basis citation appears verbatim -- never rewritten/paraphrased.
    assert GDPR_LEGAL_BASIS in event.detail["draft"]
    # The other Case-derived placeholders were filled in too, not left as literal `{...}` text.
    assert case.target_email in event.detail["draft"]
    assert case.site in event.detail["draft"]
    assert case.domain in event.detail["draft"]
    assert global_config.requester_name in event.detail["draft"]
    # None of the template's own placeholder names were left unresolved (str.format() would have
    # raised on any that were, but check explicitly rather than relying only on that side effect --
    # and unlike a blanket "no braces at all" check, this doesn't break if template content or a
    # Case-derived field legitimately contains a brace, per
    # test_draft_tolerates_braces_in_case_derived_fields below).
    for placeholder in (
        "{target_email}",
        "{site}",
        "{domain}",
        "{legal_basis}",
        "{requester_name}",
    ):
        assert placeholder not in event.detail["draft"]


async def test_draft_tolerates_braces_in_case_derived_fields(
    session: Session, legal_provider: RegistryLegalTemplateProvider, global_config: GlobalConfig
) -> None:
    """`site`/`domain`/`target_email` trace back to discovery evidence -- external, not fully
    trusted input. `str.format()` only parses `{...}` placeholders in the *template string*
    (`template.body_template`); substituted argument values are inserted verbatim and never
    re-scanned for braces, so a stray `{`/`}` in one of these fields cannot raise or be misread as
    a second placeholder -- it must appear in the final draft exactly as given, unescaped."""
    case = _make_case(
        session,
        site="Evil {legal_basis} Corp",
        applicable_jurisdiction="GDPR",
        drafting_language="en",
        state=CaseState.CLASSIFIED,
    )

    await _draft(session, case, legal_provider, global_config)
    session.commit()

    [event] = _events_for(session, case)
    assert "Evil {legal_basis} Corp" in event.detail["draft"]
    # The real legal basis still appears exactly once, from the template -- the brace-shaped text
    # inside `site` was inserted as a literal string, never treated as a second placeholder.
    assert event.detail["draft"].count(GDPR_LEGAL_BASIS) == 1


async def test_classify_raises_if_no_jurisdiction_or_language_resolvable(session: Session) -> None:
    """A `Case` with no per-case jurisdiction/language and no global default configured fails
    loud in `_classify()` rather than `_draft()` silently drafting something meaningless."""
    case = _make_case(session)
    llm = MockLLMProvider()
    unresolvable_config = GlobalConfig(
        requester_name=None,
        default_applicable_jurisdiction=None,
        default_drafting_language=None,
    )

    with pytest.raises(ValueError, match="applicable_jurisdiction"):
        await _classify(session, case, llm, unresolvable_config, model=_TEST_MODEL)


class _SpyLLMProvider(MockLLMProvider):
    """Records the `LLMRequest` of every `complete()` call, on top of `MockLLMProvider`'s
    deterministic canned response -- lets a test assert on exactly what was sent, not just that
    something was returned."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[object] = []

    async def complete(self, request):  # type: ignore[override]
        self.requests.append(request)
        return await super().complete(request)


async def test_classify_sends_the_real_resolved_model_not_a_hardcoded_placeholder(
    session: Session, global_config: GlobalConfig
) -> None:
    """Real report/regression: `_classify()` used to hardcode a literal `"mock-classify"` model
    name in the `LLMRequest` it sends, regardless of what `llm` actually is -- invisible against
    `MockLLMProvider` (which ignores `request.model`), but a real Anthropic/OpenAI-compatible
    adapter sends that literal straight to the vendor API, which correctly rejects it as an
    unknown model. `model` must reach the request byte-for-byte as given, proving there's no
    hardcoded value anywhere on this path anymore."""
    case = _make_case(session)
    llm = _SpyLLMProvider()

    await _classify(session, case, llm, global_config, model="claude-sonnet-5")

    assert len(llm.requests) == 1
    assert llm.requests[0].model == "claude-sonnet-5"


async def test_draft_raises_lookuperror_with_case_context_for_unregistered_jurisdiction(
    session: Session, legal_provider: RegistryLegalTemplateProvider, global_config: GlobalConfig
) -> None:
    """A jurisdiction/language pair that resolves (not `None`) but isn't actually registered in
    `LegalTemplateProvider` (e.g. a typo'd default) must fail with case context, not a bare
    `LookupError` naming neither the case nor where to look -- consistent with `_classify()`'s
    own resolution-failure message."""
    case = _make_case(
        session,
        applicable_jurisdiction="NOT-A-REAL-JURISDICTION",
        drafting_language="en",
        state=CaseState.CLASSIFIED,
    )

    with pytest.raises(LookupError, match=f"case {case.id}.*NOT-A-REAL-JURISDICTION"):
        await _draft(session, case, legal_provider, global_config)

    # The case is left exactly as it was: still Classified, no draft_proposed Event recorded, so
    # it can be retried once the misconfigured jurisdiction is fixed.
    assert case.state == CaseState.CLASSIFIED
    assert _events_for(session, case) == []
