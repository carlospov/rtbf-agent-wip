"""Tests for `rtbf_agent.cli` -- the real CLI orchestration (issue #36).

Covers the restrictions this module discharges (`docs/planning/RESTRICTIONS.md`):
R1 (no code path here can reach `send_request()` for a `Case` outside `Draft ready`), R9 (a
second invocation over the same database must not re-notify or re-send for cases already past
`Draft ready`).

No real Gmail account, LLM vendor, SMTP server, or interactive terminal is used anywhere here --
`run_once()` takes every collaborator as an injected fake/stub, following this repo's established
pattern (see `tests/test_agent_loop_mock.py`, `tests/plugins/senders/test_smtp.py`).
"""

from __future__ import annotations

import importlib.metadata
import json
from collections.abc import Sequence

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from rtbf_agent import case_actions
from rtbf_agent import cli as cli_module
from rtbf_agent.cli import (
    REGISTRATION_CHECK_KEY,
    add_case_for_url,
    create_cases_for_new_accounts,
    extract_domain,
    run_once,
)
from rtbf_agent.config import GlobalConfig
from rtbf_agent.core.interfaces import (
    DiscoveredAccount,
    DiscoverySource,
    EmailMessage,
    SearchResult,
    Sender,
)
from rtbf_agent.core.models import Case, CaseState, Document, DocumentStatus, Event, create_event
from rtbf_agent.followup import draft_additional_info_response
from rtbf_agent.plugins.followup.detector import ResponseClassifier
from rtbf_agent.plugins.followup.monitor import InboxReplyMonitor, MailboxMessage
from rtbf_agent.plugins.jurisdiction_detection.detector import (
    JurisdictionAttempt,
    JurisdictionProposal,
)
from rtbf_agent.plugins.legal_templates.provider import RegistryLegalTemplateProvider
from rtbf_agent.plugins.llm.config import LLMConfig
from rtbf_agent.plugins.llm.mock import MockLLMProvider
from rtbf_agent.plugins.notifiers.console import ConsoleNotifier
from rtbf_agent.plugins.notifiers.telegram import TelegramNotifier
from rtbf_agent.plugins.recipient_detection.detector import RecipientAttempt, RecipientProposal


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
    return GlobalConfig(
        requester_name="Ada Lovelace",
        default_applicable_jurisdiction="GDPR",
        default_drafting_language="en",
    )


class _FakeDiscoverySource(DiscoverySource):
    """Returns a fixed, injected sequence of `DiscoveredAccount`s -- never touches a real inbox."""

    def __init__(self, accounts: Sequence[DiscoveredAccount]) -> None:
        self._accounts = tuple(accounts)

    async def discover(self, target_email: str) -> Sequence[DiscoveredAccount]:
        return self._accounts


class _SpySender(Sender):
    """Records every `send()` call -- a test fails if this is invoked when it shouldn't be."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


class _FakeIO:
    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.printed: list[str] = []

    def input_fn(self, prompt: str) -> str:
        return self._answers.pop(0)

    def print_fn(self, text: str) -> None:
        self.printed.append(text)


def _account(site: str, domain: str, target_email: str = "user@example.com") -> DiscoveredAccount:
    return DiscoveredAccount(
        site=site,
        domain=domain,
        target_email=target_email,
        evidence={"email_id": f"msg-{domain}", "matched_pattern": "welcome_email"},
    )


# --- discovery -> case creation, dedup -------------------------------------------------------


def test_cli_creates_case_for_each_new_discovered_account_and_skips_existing_ones(
    session: Session,
) -> None:
    # An existing Case already represents example.com for this target email.
    existing = Case(site="Example", domain="example.com", target_email="user@example.com")
    session.add(existing)
    session.commit()

    accounts = [
        _account("Example", "example.com"),  # already has a Case -- must be skipped
        _account("New Site", "newsite.com"),  # genuinely new -- must create a Case
    ]

    created = create_cases_for_new_accounts(session, accounts)
    session.commit()

    assert [c.domain for c in created] == ["newsite.com"]

    all_cases = session.exec(select(Case)).all()
    domains = sorted(c.domain for c in all_cases)
    assert domains == ["example.com", "newsite.com"]
    # The pre-existing Case for example.com was left untouched, not duplicated.
    assert sum(1 for c in all_cases if c.domain == "example.com") == 1

    new_case = next(c for c in all_cases if c.domain == "newsite.com")
    assert new_case.target_email == "user@example.com"
    assert new_case.state == CaseState.DISCOVERED
    assert new_case.discovery_evidence is not None
    assert "msg-newsite.com" in new_case.discovery_evidence


def test_cli_dedup_also_applies_within_a_single_discover_batch(session: Session) -> None:
    """Two DiscoveredAccounts for the same (domain, target_email) in one batch must not create
    two Cases -- GmailDiscoverySource already deduplicates by domain internally, but this is a
    second, independent guard at the orchestration layer."""
    accounts = [_account("Site A", "same.com"), _account("Site A (dup)", "same.com")]

    created = create_cases_for_new_accounts(session, accounts)
    session.commit()

    assert len(created) == 1


# --- full run_once(): discover -> loop -> approval -> send, and repeat-run safety (R9) -------


async def test_run_once_end_to_end_creates_case_drafts_and_sends_on_approval(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """The two-phase, non-blocking loop: one `run_once()` pass discovers/classifies/drafts and
    leaves the case at `Draft ready` (no live approval/recipient prompt exists anymore); a human
    then records the recipient + approval via `case_actions` (kanban or CLI, simulated directly
    here); a second `run_once()` pass picks up the recorded decisions and actually sends -- exactly
    mirroring `tests/test_followup_resend.py`'s established pattern for this rework."""
    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    llm = MockLLMProvider()
    # The only two answers are for the new Case's jurisdiction/drafting-language prompt (blank =
    # defer to global default) -- recipient confirmation and send approval are no longer solicited
    # live inside run_once() at all.
    io = _FakeIO(answers=["", ""])
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
    )

    case = session.exec(select(Case)).one()
    assert case.state == CaseState.DRAFT_READY
    assert sender.sent == []  # not sent yet -- no recipient/approval recorded

    case_actions.confirm_recipient(session, case.id, "privacy@example-corp.com")
    case_actions.approve_draft(session, case.id)
    session.commit()

    await run_once(
        session,
        None,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
    )
    session.refresh(case)

    assert case.state == CaseState.AWAITING_RESPONSE
    assert case.recipient_email == "privacy@example-corp.com"
    assert len(sender.sent) == 1
    # The request went to the site's real recipient, not the user's own scanned mailbox.
    assert sender.sent[0].to_address == "privacy@example-corp.com"
    assert sender.sent[0].to_address != "user@example.com"

    kinds = [e.kind for e in session.exec(select(Event).where(Event.case_id == case.id)).all()]
    assert "approval_granted" in kinds
    assert "request_sent" in kinds


class _FakeMailboxReader:
    """Returns injected reply messages, ignoring the query -- no real Gmail."""

    def __init__(self, messages: Sequence[MailboxMessage]) -> None:
        self._messages = {message.message_id: message for message in messages}

    def list_message_ids(self, query: str) -> Sequence[str]:
        return list(self._messages)

    def get_message(self, message_id: str) -> MailboxMessage:
        return self._messages[message_id]


class _CannedLLMProvider(MockLLMProvider):
    """A `MockLLMProvider` whose `complete()` returns a fixed intent-JSON, for reply classification.

    Subclasses the repo's `MockLLMProvider` so the classify/draft path still works; only the reply-
    classification `complete()` output is forced to a specific intent.
    """

    def __init__(self, content: str) -> None:
        super().__init__()
        self._content = content

    async def complete(self, request):  # type: ignore[override]
        from rtbf_agent.core.interfaces import LLMResponse

        return LLMResponse(content=self._content, model=request.model)


async def test_run_once_follow_up_pass_moves_a_sent_case_to_deletion_confirmed_on_confirmation(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """End-to-end wiring: discover -> draft -> (recorded decisions) -> send -> follow-up ->
    auto-close. `run_once()`'s follow-up pass now closes a case to `Deletion confirmed` itself as
    soon as a reply is classified as a CONFIRMATION -- no separate human `case_actions.
    confirm_followup()` call needed (maintainer-directed narrowing of issue #68; see
    `followup.py`'s module docstring, "Why CONFIRMATION auto-closes now")."""
    from datetime import UTC, datetime, timedelta

    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    io = _FakeIO(answers=["", ""])  # jurisdiction, drafting language
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    reply = MailboxMessage(
        message_id="reply-1",
        from_address="Support <support@example-corp.com>",
        subject="Re: your deletion request",
        body="We have deleted your account and all associated data.",
        received_at=datetime.now(UTC) + timedelta(hours=1),
    )
    reply_monitor = InboxReplyMonitor(_FakeMailboxReader([reply]))
    response_classifier = ResponseClassifier(
        _CannedLLMProvider(json.dumps({"intent": "confirmation", "reasoning": "Deleted."})),
        model="mock",
    )

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        MockLLMProvider(),
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
    )
    case = session.exec(select(Case)).one()
    assert case.state == CaseState.DRAFT_READY

    case_actions.confirm_recipient(session, case.id, "privacy@example-corp.com")
    case_actions.approve_draft(session, case.id)
    session.commit()

    await run_once(
        session,
        None,
        "user@example.com",
        MockLLMProvider(),
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        reply_monitor=reply_monitor,
        response_classifier=response_classifier,
        model="test-model",
    )
    session.refresh(case)

    # Sent and the reply classified as a confirmation -- closed immediately, agent-authored, no
    # separate human confirm_followup() call needed.
    assert case.state == CaseState.DELETION_CONFIRMED
    kinds = [e.kind for e in session.exec(select(Event).where(Event.case_id == case.id)).all()]
    assert "response_classified" in kinds
    assert "followup_confirmed" in kinds


async def test_run_once_without_follow_up_collaborators_leaves_a_sent_case_at_sent(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """Omitting the follow-up collaborators (every pre-issue-#68 caller) skips follow-up entirely --
    the case stops at `Awaiting response` (the state a successful send always lands on, replacing
    the old, terminal `Sent`), exactly as it would with follow-up enabled but no reply yet."""
    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    io = _FakeIO(answers=["", ""])
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        MockLLMProvider(),
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
    )
    case = session.exec(select(Case)).one()
    case_actions.confirm_recipient(session, case.id, "privacy@example-corp.com")
    case_actions.approve_draft(session, case.id)
    session.commit()

    await run_once(
        session,
        None,
        "user@example.com",
        MockLLMProvider(),
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
    )

    session.refresh(case)
    assert case.state == CaseState.AWAITING_RESPONSE


class _RaisingForDomainMailboxReader:
    """Raises for a specific sender-domain query, returns injected messages for any other.

    Simulates a per-case transport failure (e.g. a Gmail API error scanning one case's expected
    reply domain) without affecting any other case's monitoring pass in the same run."""

    def __init__(self, raising_query: str, messages: Sequence[MailboxMessage]) -> None:
        self._raising_query = raising_query
        self._messages = {message.message_id: message for message in messages}

    def list_message_ids(self, query: str) -> Sequence[str]:
        if query == self._raising_query:
            raise RuntimeError("simulated Gmail API error for this domain")
        return list(self._messages)

    def get_message(self, message_id: str) -> MailboxMessage:
        return self._messages[message_id]


async def test_run_once_follow_up_pass_continues_past_one_case_transport_failure(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """A `MailboxReader`/LLM transport failure while following up one case must not abort the whole
    pass -- every other monitored case still gets its own follow_up() attempt in the same run."""
    from datetime import UTC, datetime, timedelta

    discovery_source = _FakeDiscoverySource(
        [_account("Broken", "broken.example"), _account("Working", "working.example")]
    )
    # run_once() prompts jurisdiction/language for every new case (creation order) -- recipient
    # confirmation and send approval are recorded separately via case_actions below, not prompted.
    io = _FakeIO(answers=["", "", "", ""])
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        MockLLMProvider(),
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
    )
    cases = {case.domain: case for case in session.exec(select(Case)).all()}
    for domain, case in cases.items():
        case_actions.confirm_recipient(session, case.id, f"privacy@{domain}")
        case_actions.approve_draft(session, case.id)
    session.commit()

    reply = MailboxMessage(
        message_id="reply-1",
        from_address="Support <support@working.example>",
        subject="Re: your deletion request",
        body="We have deleted your account and all associated data.",
        received_at=datetime.now(UTC) + timedelta(hours=1),
    )
    # broken.example's own domain-scoped query always raises; working.example's succeeds.
    reply_monitor = InboxReplyMonitor(
        _RaisingForDomainMailboxReader("from:broken.example", [reply])
    )
    response_classifier = ResponseClassifier(
        _CannedLLMProvider(json.dumps({"intent": "confirmation", "reasoning": "Deleted."})),
        model="mock",
    )

    await run_once(
        session,
        None,
        "user@example.com",
        MockLLMProvider(),
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        reply_monitor=reply_monitor,
        response_classifier=response_classifier,
        model="test-model",
    )

    for case in cases.values():
        session.refresh(case)
    # broken.example's follow-up attempt failed and was skipped, not crashed -- still Awaiting
    # response.
    assert cases["broken.example"].state == CaseState.AWAITING_RESPONSE
    # working.example's reply was found, classified as a confirmation, and auto-closed in the same
    # invocation -- no separate case_actions.confirm_followup() call needed.
    assert cases["working.example"].state == CaseState.DELETION_CONFIRMED
    working_kinds = [
        e.kind
        for e in session.exec(
            select(Event).where(Event.case_id == cases["working.example"].id)
        ).all()
    ]
    assert "response_classified" in working_kinds
    assert "followup_confirmed" in working_kinds


async def test_run_once_sends_a_case_approved_from_a_pending_additional_info_draft(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """`run_once()`'s existing "send every approved Draft ready case" pass, exercised for the
    drafting-is-a-preview redesign's flow (superseding issue #117/#119's fused, poll-triggered
    draft+send): a case in `Requires human action` with a recorded `case_actions.
    provide_text_response()` answer, explicitly drafted (`followup.
    draft_additional_info_response()`, the kanban "Draft" button/CLI subcommand) and then approved
    (`case_actions.approve_draft()`, which flips it to `Draft ready`) gets picked up and sent by
    this same, unmodified pass -- no separate additional-info pass in `run_once()` itself
    anymore."""
    case = Case(
        site="Example Corp",
        domain="example.com",
        target_email="user@example.com",
        recipient_email="privacy@example-corp.com",
        applicable_jurisdiction="GDPR",
        drafting_language="en",
        state=CaseState.REQUIRES_HUMAN_ACTION,
    )
    session.add(case)
    session.commit()
    session.refresh(case)
    assert case.id is not None
    session.add(
        create_event(
            case_id=case.id,
            kind="draft_proposed",
            actor="agent",
            detail={
                "draft": "Please delete my personal data pursuant to GDPR Art. 17.",
                "subject": "Deletion request for example.com",
                "jurisdiction": "GDPR",
                "drafting_language": "en",
            },
        )
    )
    session.add(create_event(case_id=case.id, kind="request_sent", actor="agent"))
    session.add(
        create_event(
            case_id=case.id,
            kind="additional_info_requested",
            actor="agent",
            detail={"reasoning": "site asked for date of birth"},
        )
    )
    session.commit()

    case_actions.provide_text_response(session, case.id, "my date of birth is 1990-01-01")
    await draft_additional_info_response(session, case, MockLLMProvider(), model="test-model")
    session.commit()
    case_actions.approve_draft(session, case.id)
    session.refresh(case)
    assert case.state == CaseState.DRAFT_READY

    notifier = ConsoleNotifier(print_fn=lambda _text: None)
    sender = _SpySender()

    await run_once(
        session,
        None,
        "user@example.com",
        MockLLMProvider(),
        notifier,
        legal_provider,
        global_config,
        sender,
        model="test-model",
    )

    session.refresh(case)
    assert case.state == CaseState.AWAITING_RESPONSE
    assert len(sender.sent) == 1
    assert "1990-01-01" in sender.sent[0].body


async def test_run_once_sends_the_attachment_for_a_case_approved_from_a_pending_document(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Real report: a `Draft ready` case reached via `case_actions.attach_document()` -> the
    additional-info draft/approve path got its `request_sent` on *this* pass's generic "send every
    approved draft" loop, which called `send_request()` with no `document` argument at all -- the
    resend went out with no attachment, silently, every time, while the `Document` stayed
    `Awaiting approval` forever (never `Attached`, no `document_attached` Event, no secure-delete).
    `run_once()` must look the pending document back up before sending an approved `Draft ready`
    case, not just for the same-pass draft-creation call that always no-ops for lack of an approval
    yet (`agent_loop.compose_additional_info_draft()`'s own docstring)."""
    monkeypatch.setenv(
        "RTBF_DOCUMENT_ENCRYPTION_KEY", "zJ8gk8h1qYw3F1v3l4v0m9C7b3e9r0g5t9p9nQe8s0A="
    )
    monkeypatch.setenv("RTBF_DOCUMENT_STORAGE_DIR", str(tmp_path / "documents"))

    case = Case(
        site="Example Corp",
        domain="example.com",
        target_email="user@example.com",
        recipient_email="privacy@example-corp.com",
        applicable_jurisdiction="GDPR",
        drafting_language="en",
        state=CaseState.REQUIRES_HUMAN_ACTION,
    )
    session.add(case)
    session.commit()
    session.refresh(case)
    assert case.id is not None
    session.add(
        create_event(
            case_id=case.id,
            kind="draft_proposed",
            actor="agent",
            detail={
                "draft": "Please delete my personal data pursuant to GDPR Art. 17.",
                "subject": "Deletion request for example.com",
                "jurisdiction": "GDPR",
                "drafting_language": "en",
            },
        )
    )
    session.add(create_event(case_id=case.id, kind="request_sent", actor="agent"))
    session.add(
        create_event(
            case_id=case.id,
            kind="additional_info_requested",
            actor="agent",
            detail={"reasoning": "site asked for a photo ID"},
        )
    )
    session.commit()

    case_actions.attach_document(session, case.id, b"fake id card bytes", "image/png")
    await draft_additional_info_response(session, case, MockLLMProvider(), model="test-model")
    session.commit()
    case_actions.approve_draft(session, case.id)
    session.refresh(case)
    assert case.state == CaseState.DRAFT_READY

    notifier = ConsoleNotifier(print_fn=lambda _text: None)
    sender = _SpySender()

    await run_once(
        session,
        None,
        "user@example.com",
        MockLLMProvider(),
        notifier,
        legal_provider,
        global_config,
        sender,
        model="test-model",
    )

    session.refresh(case)
    assert case.state == CaseState.AWAITING_RESPONSE
    assert len(sender.sent) == 1
    assert len(sender.sent[0].attachments) == 1
    assert sender.sent[0].attachments[0].content == b"fake id card bytes"

    document = session.exec(select(Document).where(Document.case_id == case.id)).one()
    assert document.status == DocumentStatus.AVAILABLE  # issue #111: survives being attached
    kinds = [event.kind for event in session.exec(select(Event).where(Event.case_id == case.id))]
    assert "document_attached" in kinds


async def test_cli_repeated_invocation_does_not_resend_already_sent_cases(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    llm = MockLLMProvider()
    io = _FakeIO(answers=["", ""])
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
    )
    case = session.exec(select(Case)).one()
    case_actions.confirm_recipient(session, case.id, "privacy@example-corp.com")
    case_actions.approve_draft(session, case.id)
    session.commit()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
    )
    assert len(sender.sent) == 1
    session.refresh(case)
    assert case.state == CaseState.AWAITING_RESPONSE
    events_after_send = session.exec(select(Event)).all()

    # A third invocation over the same database/discovery results must not re-create a Case
    # (dedup), must not prompt again for jurisdiction/drafting language (the case already exists,
    # so it's never re-proposed to `create_cases_for_new_accounts()` as new -- no more answers
    # queued; a re-prompt of any kind would raise IndexError on `.pop(0)` of an empty list) -- and
    # must not re-send (R9): the case is already past `Draft ready`, so it's outside both the
    # `run_cycle()` batch and the send loop.
    await run_once(
        session,
        discovery_source,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
    )

    all_cases = session.exec(select(Case)).all()
    assert len(all_cases) == 1
    assert all_cases[0].state == CaseState.AWAITING_RESPONSE
    assert len(sender.sent) == 1  # still just the one send
    assert session.exec(select(Event)).all() == events_after_send


async def test_cli_second_run_does_not_touch_a_rejected_case(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    llm = MockLLMProvider()
    io = _FakeIO(answers=["", ""])  # jurisdiction, drafting language
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
    )
    case = session.exec(select(Case)).one()
    assert case.state == CaseState.DRAFT_READY

    case_actions.reject_draft(session, case.id)
    session.commit()
    session.refresh(case)
    assert case.state == CaseState.REJECTED_ESCALATED
    assert sender.sent == []

    # A second run must not re-prompt (no answers queued, for jurisdiction/language) or send for
    # the rejected case -- it's outside both the run_cycle() batch and the send loop.
    await run_once(
        session,
        discovery_source,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
    )

    case_after = session.exec(select(Case)).one()
    assert case_after.state == CaseState.REJECTED_ESCALATED
    assert sender.sent == []


# --- auto-apply jurisdiction / no-prompt-at-creation (issue #43 reversal) ---------------------


def test_apply_jurisdiction_proposal_writes_a_genuine_proposal_with_no_prompt() -> None:
    """`_apply_jurisdiction_proposal()` -- the direct, non-interactive replacement for
    `prompt_case_overrides()` -- writes a genuine proposal straight onto `Case.
    applicable_jurisdiction`, no `input_fn`/confirmation step involved."""
    case = Case(site="Example", domain="example.com", target_email="user@example.com")
    proposal = JurisdictionProposal(jurisdiction="GDPR", reasoning="x", sources=())

    cli_module._apply_jurisdiction_proposal(case, proposal)

    assert case.applicable_jurisdiction == "GDPR"


def test_apply_jurisdiction_proposal_leaves_the_field_unset_without_a_proposal() -> None:
    """No proposal (detection disabled, or a detector that never resolved a confident one) leaves
    `applicable_jurisdiction` `None` -- silently deferring to the global default later, exactly as
    it always did, never a crash or a stale value."""
    case = Case(site="Example", domain="example.com", target_email="user@example.com")

    cli_module._apply_jurisdiction_proposal(case, None)

    assert case.applicable_jurisdiction is None


async def test_cli_auto_applies_jurisdiction_proposal_on_new_case_creation_with_no_prompt(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """End-to-end through `run_once()`: a brand-new Case with a genuine jurisdiction proposal gets
    it auto-applied with zero `input_fn` calls -- the maintainer-directed reversal of issue #43's
    original manual-prompt design. The proposed jurisdiction (`GDPR`) survives `_classify()`'s own
    resolve-and-lock step (`Case.resolved_applicable_jurisdiction` prefers a non-`None` per-case
    value over the global default), which is what proves it was actually applied at creation time,
    not merely coincidentally matching the global default (fixture default is deliberately `CCPA`,
    distinct from the proposal, for exactly this reason)."""
    distinct_global_config = GlobalConfig(
        requester_name=global_config.requester_name,
        default_applicable_jurisdiction="CCPA",
        default_drafting_language=global_config.default_drafting_language,
    )
    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    llm = MockLLMProvider()
    detector = _FakeJurisdictionDetector(
        proposal=JurisdictionProposal(jurisdiction="GDPR", reasoning="x", sources=())
    )
    io = _FakeIO(answers=[])  # no prompt should ever consume an answer
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        distinct_global_config,
        sender,
        jurisdiction_detector=detector,
        print_fn=io.print_fn,
        model="test-model",
    )

    case = session.exec(select(Case)).one()
    assert case.applicable_jurisdiction == "GDPR"  # the proposal, not the "CCPA" global default
    events = session.exec(select(Event).where(Event.case_id == case.id)).all()
    assert any(e.kind == "jurisdiction_proposed" for e in events)


def test_cli_set_jurisdiction_main_updates_a_discovered_case(session: Session) -> None:
    """`case_actions.set_case_jurisdiction()`, backing the shell's `set-jurisdiction` command --
    the review/override path replacing the old creation-time prompt."""
    case = Case(site="Example", domain="example.com", target_email="user@example.com")
    session.add(case)
    session.commit()
    session.refresh(case)

    updated = case_actions.set_case_jurisdiction(session, case.id, "CCPA")

    assert updated.applicable_jurisdiction == "CCPA"
    events = session.exec(select(Event).where(Event.case_id == case.id)).all()
    assert any(e.kind == "jurisdiction_overridden" for e in events)


def test_cli_set_drafting_language_main_updates_a_discovered_case(session: Session) -> None:
    """`case_actions.set_case_drafting_language()`, backing the shell's `set-drafting-language`
    command."""
    case = Case(site="Example", domain="example.com", target_email="user@example.com")
    session.add(case)
    session.commit()
    session.refresh(case)

    updated = case_actions.set_case_drafting_language(session, case.id, "fr")

    assert updated.drafting_language == "fr"
    events = session.exec(select(Event).where(Event.case_id == case.id)).all()
    assert any(e.kind == "drafting_language_overridden" for e in events)


def test_cli_set_jurisdiction_rejects_a_case_past_discovered(session: Session) -> None:
    case = Case(
        site="Example",
        domain="example.com",
        target_email="user@example.com",
        state=CaseState.CLASSIFIED,
    )
    session.add(case)
    session.commit()
    session.refresh(case)

    with pytest.raises(case_actions.InvalidCaseStateError):
        case_actions.set_case_jurisdiction(session, case.id, "GDPR")


def test_cli_set_drafting_language_rejects_a_case_past_discovered(session: Session) -> None:
    """R22 parity with the jurisdiction test above -- drafting language gets the identical
    Discovered-only gate, on its own, independent of jurisdiction."""
    case = Case(
        site="Example",
        domain="example.com",
        target_email="user@example.com",
        state=CaseState.CLASSIFIED,
    )
    session.add(case)
    session.commit()
    session.refresh(case)

    with pytest.raises(case_actions.InvalidCaseStateError):
        case_actions.set_case_drafting_language(session, case.id, "fr")


# --- RTBF_NOTIFIER_BACKEND / TelegramNotifier wiring (issue #60) ----------------------------
#
# No real `python-telegram-bot` process, bot token, or network call is used anywhere below --
# `PythonTelegramBotClient` is monkeypatched to a fake, mirroring how the gmail OAuth tests above
# fake out `google_auth_oauthlib`/`googleapiclient` collaborators.
#
# Automated-case-loop rework: `TelegramNotifier` is notify-only now (a single outbound
# `sendMessage` call) -- there is no `Application`/polling lifecycle left to start/stop around
# `run_once()` (see `TelegramNotifier`'s module docstring and `_resolve_notifier_and_run()`'s), so
# the `_FakeApplication`/`_FakeUpdater` fakes and the lifecycle-ordering tests that drove them
# (`test_cli_starts_and_stops_telegram_polling_around_run_once`,
# `test_cli_stops_telegram_polling_even_if_run_once_raises`,
# `test_cli_telegram_shuts_down_even_if_start_raises`,
# `test_cli_telegram_tears_down_even_if_start_polling_raises`) are deleted outright rather than
# ported -- there is no lifecycle left for them to exercise. What remains below is just "which
# notifier does the resolved backend produce", still real coverage.


def _set_fake_telegram_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RTBF_NOTIFIER_BACKEND", "telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setenv("INTERFACE_LANGUAGE", "en")


async def test_cli_defaults_to_console_notifier_when_backend_unset(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No behavior change for existing console-only usage: `RTBF_NOTIFIER_BACKEND` unset must
    resolve to `ConsoleNotifier`, and the Telegram config resolver must never even be called."""
    monkeypatch.delenv("RTBF_NOTIFIER_BACKEND", raising=False)

    def _must_not_be_called() -> None:
        raise AssertionError("resolve_telegram_config() must not be called for the console backend")

    monkeypatch.setattr(cli_module, "resolve_telegram_config", lambda: _must_not_be_called())

    captured: dict[str, object] = {}

    async def _fake_run_once(
        session_,
        discovery_source_,
        target_email_,
        llm_,
        notifier_,
        legal_provider_,
        global_config_,
        sender_,
        **_kwargs,
    ) -> None:
        captured["notifier"] = notifier_

    monkeypatch.setattr(cli_module, "run_once", _fake_run_once)

    await cli_module._run_with_configured_notifier(
        session,
        _FakeDiscoverySource([]),
        "user@example.com",
        MockLLMProvider(),
        legal_provider,
        global_config,
        _SpySender(),
        "test-model",
    )

    assert isinstance(captured["notifier"], ConsoleNotifier)


async def test_cli_uses_telegram_notifier_when_backend_configured(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`RTBF_NOTIFIER_BACKEND=telegram` must make `run_once()` receive a real `TelegramNotifier`,
    not `ConsoleNotifier` -- with no real bot token or Telegram API call anywhere in this test."""
    _set_fake_telegram_env(monkeypatch)
    monkeypatch.setattr(cli_module, "PythonTelegramBotClient", lambda token: object())

    captured: dict[str, object] = {}

    async def _fake_run_once(
        session_,
        discovery_source_,
        target_email_,
        llm_,
        notifier_,
        legal_provider_,
        global_config_,
        sender_,
        **_kwargs,
    ) -> None:
        captured["notifier"] = notifier_

    monkeypatch.setattr(cli_module, "run_once", _fake_run_once)

    await cli_module._run_with_configured_notifier(
        session,
        _FakeDiscoverySource([]),
        "user@example.com",
        MockLLMProvider(),
        legal_provider,
        global_config,
        _SpySender(),
        "test-model",
    )

    assert isinstance(captured["notifier"], TelegramNotifier)


def test_resolve_notifier_backend_defaults_to_console_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RTBF_NOTIFIER_BACKEND", raising=False)
    assert cli_module._resolve_notifier_backend() == "console"


def test_resolve_notifier_backend_rejects_unsupported_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RTBF_NOTIFIER_BACKEND", "sms")
    with pytest.raises(ValueError, match="unsupported RTBF_NOTIFIER_BACKEND"):
        cli_module._resolve_notifier_backend()


# --- RTBF_REPLY_MONITOR_BACKEND / ImapReplyReader wiring (issue #102) -----------------------
#
# Mirrors the RTBF_NOTIFIER_BACKEND tests just above -- no real IMAP server, Gmail OAuth flow, or
# network call is used anywhere below.


def test_resolve_reply_monitor_backend_defaults_to_gmail_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RTBF_REPLY_MONITOR_BACKEND", raising=False)
    assert cli_module._resolve_reply_monitor_backend() == "gmail"


def test_resolve_reply_monitor_backend_rejects_unsupported_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RTBF_REPLY_MONITOR_BACKEND", "pop3")
    with pytest.raises(ValueError, match="unsupported RTBF_REPLY_MONITOR_BACKEND"):
        cli_module._resolve_reply_monitor_backend()


def test_build_reply_monitor_uses_gmail_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RTBF_REPLY_MONITOR_BACKEND", raising=False)
    sentinel = object()
    monkeypatch.setattr(cli_module, "_build_gmail_reply_monitor", lambda: sentinel)

    def _must_not_build_imap() -> None:
        raise AssertionError("must not build the IMAP reply monitor when the backend is gmail")

    monkeypatch.setattr(cli_module, "_build_imap_reply_monitor", _must_not_build_imap)

    assert cli_module._build_reply_monitor() is sentinel


def test_build_reply_monitor_uses_imap_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RTBF_REPLY_MONITOR_BACKEND", "imap")
    sentinel = object()
    monkeypatch.setattr(cli_module, "_build_imap_reply_monitor", lambda: sentinel)

    def _must_not_build_gmail() -> None:
        raise AssertionError("must not build the Gmail reply monitor when the backend is imap")

    monkeypatch.setattr(cli_module, "_build_gmail_reply_monitor", _must_not_build_gmail)

    assert cli_module._build_reply_monitor() is sentinel


def test_build_imap_reply_monitor_wires_resolved_config_into_the_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_build_imap_reply_monitor()` must build an `InboxReplyMonitor` over a real
    `SubprocessImapReplyReader` constructed from exactly what `resolve_imap_config()` resolved --
    no real IMAP connection (nor any real subprocess) is opened here
    (`SubprocessImapReplyReader.__init__` never touches the network or spawns anything)."""
    from rtbf_agent.plugins.followup import imap_config as imap_config_module
    from rtbf_agent.plugins.followup.imap_reader import SubprocessImapReplyReader

    monkeypatch.setattr(
        imap_config_module,
        "resolve_imap_config",
        lambda: imap_config_module.ImapConfig(
            host="imap.example.com", port=993, username="carlos", password="sk-test-password"
        ),
    )

    monitor = cli_module._build_imap_reply_monitor()

    assert isinstance(monitor, InboxReplyMonitor)
    reader = monitor._reader
    assert isinstance(reader, SubprocessImapReplyReader)
    assert reader._host == "imap.example.com"
    assert reader._port == 993
    assert reader._username == "carlos"
    assert reader._password == "sk-test-password"


async def test_process_main_async_attempts_imap_follow_up_unconditionally(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Unlike Gmail, `RTBF_REPLY_MONITOR_BACKEND=imap` needs no cached-credentials gate: it must be
    attempted every pass with no interactive handshake involved (issue #102)."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("RTBF_TARGET_EMAIL", "user@example.com")
    monkeypatch.setenv("RTBF_REPLY_MONITOR_BACKEND", "imap")
    monkeypatch.delenv("RTBF_GMAIL_CLIENT_SECRETS_FILE", raising=False)
    monkeypatch.delenv("RTBF_NOTIFIER_BACKEND", raising=False)
    monkeypatch.delenv("RTBF_SEARXNG_BASE_URL", raising=False)
    monkeypatch.delenv("RTBF_SEARXNG_API_KEY", raising=False)

    def _must_not_check_gmail_cache() -> bool:
        raise AssertionError(
            "must not consult Gmail's noninteractive-credentials cache for the imap backend"
        )

    monkeypatch.setattr(
        cli_module, "_gmail_credentials_available_noninteractively", _must_not_check_gmail_cache
    )
    fake_reply_monitor = object()
    fake_response_classifier = object()
    monkeypatch.setattr(cli_module, "_build_reply_monitor", lambda: fake_reply_monitor)
    monkeypatch.setattr(
        cli_module, "_build_response_classifier", lambda llm: fake_response_classifier
    )
    monkeypatch.setattr(cli_module, "_build_llm_provider", lambda: MockLLMProvider())
    monkeypatch.setattr(
        cli_module,
        "resolve_llm_config",
        lambda: LLMConfig(
            provider="anthropic",
            protocol="anthropic",
            base_url="https://api.anthropic.com",
            api_key="test-key",
            model="test-model",
        ),
    )
    monkeypatch.setattr(cli_module, "SMTPSender", lambda: _SpySender())

    captured: dict[str, object] = {}

    async def _fake_run_once(
        session_, discovery_source_, target_email_, *_args: object, **kwargs: object
    ) -> None:
        captured["reply_monitor"] = kwargs.get("reply_monitor")
        captured["response_classifier"] = kwargs.get("response_classifier")

    monkeypatch.setattr(cli_module, "run_once", _fake_run_once)

    await cli_module.process_main_async()

    assert captured["reply_monitor"] is fake_reply_monitor
    assert captured["response_classifier"] is fake_response_classifier


# --- _build_gmail_discovery_source(): OAuth token refresh/fallback -------------------------


def test_gmail_credential_refresh_failure_falls_through_to_new_oauth_flow(
    monkeypatch, tmp_path
) -> None:
    """A revoked/expired refresh token (google.auth.exceptions.RefreshError, or any other
    transport failure from credentials.refresh()) must fall through to a fresh OAuth handshake,
    not crash -- the whole point of caching the token is to skip that handshake when possible,
    not to require a human to manually delete the file to recover."""
    from google.oauth2.credentials import Credentials

    from rtbf_agent import cli as cli_module
    from rtbf_agent.plugins.discovery import gmail as gmail_module

    token_path = tmp_path / "gmail_token.json"
    token_path.write_text("{}")
    monkeypatch.setenv("RTBF_GMAIL_CLIENT_SECRETS_FILE", str(tmp_path / "client_secrets.json"))
    monkeypatch.setenv("RTBF_GMAIL_TOKEN_FILE", str(token_path))

    class _StaleCredentials:
        valid = False
        expired = True
        refresh_token = "stale-refresh-token"

        def refresh(self, request: object) -> None:
            raise RuntimeError("simulated revoked refresh token")

        def to_json(self) -> str:
            return "{}"

    class _FreshCredentials:
        def to_json(self) -> str:
            return "{}"

    monkeypatch.setattr(
        Credentials, "from_authorized_user_file", lambda *a, **k: _StaleCredentials()
    )

    new_flow_called = []

    class _FakeFlow:
        def run_local_server(self, port: int) -> _FreshCredentials:
            new_flow_called.append(True)
            return _FreshCredentials()

    monkeypatch.setattr(gmail_module, "build_installed_app_flow", lambda _path: _FakeFlow())
    monkeypatch.setattr(gmail_module, "build_gmail_client", lambda _creds: object())
    monkeypatch.setattr(gmail_module, "GmailDiscoverySource", lambda _client: object())

    cli_module._build_gmail_discovery_source()

    assert new_flow_called == [True]


# --- _gmail_credentials_available_noninteractively() (issue #86's Phase 3 follow-up) ---------


def test_gmail_credentials_available_noninteractively_false_with_no_cached_token(
    monkeypatch, tmp_path
) -> None:
    """No cached token file at all -- must report unavailable, never attempt anything interactive
    (there is no `flow`/`run_local_server` collaborator wired here at all, so a call to one would
    raise `AttributeError`/`ImportError`, not just fail an assertion)."""
    monkeypatch.setenv("RTBF_GMAIL_TOKEN_FILE", str(tmp_path / "no_such_token.json"))

    assert cli_module._gmail_credentials_available_noninteractively() is False


def test_gmail_credentials_available_noninteractively_true_with_a_valid_cached_token(
    monkeypatch, tmp_path
) -> None:
    from google.oauth2.credentials import Credentials

    token_path = tmp_path / "gmail_token.json"
    token_path.write_text("{}")
    monkeypatch.setenv("RTBF_GMAIL_TOKEN_FILE", str(token_path))

    class _ValidCredentials:
        valid = True

    monkeypatch.setattr(
        Credentials, "from_authorized_user_file", lambda *a, **k: _ValidCredentials()
    )

    assert cli_module._gmail_credentials_available_noninteractively() is True


def test_gmail_credentials_available_noninteractively_false_when_refresh_fails(
    monkeypatch, tmp_path
) -> None:
    """Mirrors `test_gmail_credential_refresh_failure_falls_through_to_new_oauth_flow()`'s scenario,
    but for the non-interactive check: a revoked/expired refresh token must report unavailable, not
    raise and not attempt an interactive flow (there is none wired here to attempt)."""
    from google.oauth2.credentials import Credentials

    token_path = tmp_path / "gmail_token.json"
    token_path.write_text("{}")
    monkeypatch.setenv("RTBF_GMAIL_TOKEN_FILE", str(token_path))

    class _StaleCredentials:
        valid = False
        expired = True
        refresh_token = "stale-refresh-token"

        def refresh(self, request: object) -> None:
            raise RuntimeError("simulated revoked refresh token")

    monkeypatch.setattr(
        Credentials, "from_authorized_user_file", lambda *a, **k: _StaleCredentials()
    )

    assert cli_module._gmail_credentials_available_noninteractively() is False


async def test_process_main_async_builds_follow_up_collaborators_when_gmail_is_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Once a cached Gmail token is available non-interactively, `process_main_async()` must wire
    real `reply_monitor`/`response_classifier` into `run_once()` -- proving `process`/`forget serve`
    (which reuse this exact function) genuinely check for replies, not just skip follow-up forever
    even once Gmail has been set up once via `scan-inbox`."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("RTBF_TARGET_EMAIL", "user@example.com")
    monkeypatch.setenv("RTBF_GMAIL_CLIENT_SECRETS_FILE", str(tmp_path / "client_secrets.json"))
    monkeypatch.delenv("RTBF_NOTIFIER_BACKEND", raising=False)
    monkeypatch.delenv("RTBF_SEARXNG_BASE_URL", raising=False)
    monkeypatch.delenv("RTBF_SEARXNG_API_KEY", raising=False)

    monkeypatch.setattr(cli_module, "_gmail_credentials_available_noninteractively", lambda: True)
    fake_reply_monitor = object()
    fake_response_classifier = object()
    monkeypatch.setattr(cli_module, "_build_reply_monitor", lambda: fake_reply_monitor)
    monkeypatch.setattr(
        cli_module, "_build_response_classifier", lambda llm: fake_response_classifier
    )
    monkeypatch.setattr(cli_module, "_build_llm_provider", lambda: MockLLMProvider())
    monkeypatch.setattr(
        cli_module,
        "resolve_llm_config",
        lambda: LLMConfig(
            provider="anthropic",
            protocol="anthropic",
            base_url="https://api.anthropic.com",
            api_key="test-key",
            model="test-model",
        ),
    )
    monkeypatch.setattr(cli_module, "SMTPSender", lambda: _SpySender())

    captured: dict[str, object] = {}

    async def _fake_run_once(
        session_, discovery_source_, target_email_, *_args: object, **kwargs: object
    ) -> None:
        captured["reply_monitor"] = kwargs.get("reply_monitor")
        captured["response_classifier"] = kwargs.get("response_classifier")

    monkeypatch.setattr(cli_module, "run_once", _fake_run_once)

    await cli_module.process_main_async()

    assert captured["reply_monitor"] is fake_reply_monitor
    assert captured["response_classifier"] is fake_response_classifier


async def test_process_main_async_skips_follow_up_when_gmail_is_not_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """No usable cached Gmail token -- `process_main_async()` must leave `reply_monitor`/
    `response_classifier` as `None` (R9: skip, never hang or crash), even though
    `RTBF_GMAIL_CLIENT_SECRETS_FILE` is configured."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("RTBF_TARGET_EMAIL", "user@example.com")
    monkeypatch.setenv("RTBF_GMAIL_CLIENT_SECRETS_FILE", str(tmp_path / "client_secrets.json"))
    monkeypatch.delenv("RTBF_NOTIFIER_BACKEND", raising=False)
    monkeypatch.delenv("RTBF_SEARXNG_BASE_URL", raising=False)
    monkeypatch.delenv("RTBF_SEARXNG_API_KEY", raising=False)

    monkeypatch.setattr(cli_module, "_gmail_credentials_available_noninteractively", lambda: False)

    def _must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("must not build follow-up collaborators with no cached Gmail token")

    monkeypatch.setattr(cli_module, "_build_reply_monitor", _must_not_run)
    monkeypatch.setattr(cli_module, "_build_response_classifier", _must_not_run)
    monkeypatch.setattr(cli_module, "_build_llm_provider", lambda: MockLLMProvider())
    monkeypatch.setattr(
        cli_module,
        "resolve_llm_config",
        lambda: LLMConfig(
            provider="anthropic",
            protocol="anthropic",
            base_url="https://api.anthropic.com",
            api_key="test-key",
            model="test-model",
        ),
    )
    monkeypatch.setattr(cli_module, "SMTPSender", lambda: _SpySender())

    captured: dict[str, object] = {}

    async def _fake_run_once(
        session_, discovery_source_, target_email_, *_args: object, **kwargs: object
    ) -> None:
        captured["reply_monitor"] = kwargs.get("reply_monitor")
        captured["response_classifier"] = kwargs.get("response_classifier")

    monkeypatch.setattr(cli_module, "run_once", _fake_run_once)

    await cli_module.process_main_async()

    assert captured["reply_monitor"] is None
    assert captured["response_classifier"] is None


async def test_process_main_async_warns_when_gmail_configured_but_no_token_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """Real report (issue #99): a case sent and awaiting response had a genuine reply sitting in
    the mailbox, but the follow-up pass never ran at all -- because `RTBF_GMAIL_CLIENT_SECRETS_FILE`
    was set yet no cached Gmail OAuth token existed (the operator only ever ran `process`/`serve`,
    never the interactive `scan-inbox` that establishes one) -- and this was completely silent, so
    it looked identical to "monitoring ran and found nothing". `process_main_async()` must now warn
    every time this happens, so the gap is detectable instead of indistinguishable from a genuine
    "no reply yet"."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("RTBF_TARGET_EMAIL", "user@example.com")
    monkeypatch.setenv("RTBF_GMAIL_CLIENT_SECRETS_FILE", str(tmp_path / "client_secrets.json"))
    monkeypatch.delenv("RTBF_NOTIFIER_BACKEND", raising=False)
    monkeypatch.delenv("RTBF_SEARXNG_BASE_URL", raising=False)
    monkeypatch.delenv("RTBF_SEARXNG_API_KEY", raising=False)

    monkeypatch.setattr(cli_module, "_gmail_credentials_available_noninteractively", lambda: False)

    def _must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("must not build follow-up collaborators with no cached Gmail token")

    monkeypatch.setattr(cli_module, "_build_reply_monitor", _must_not_run)
    monkeypatch.setattr(cli_module, "_build_response_classifier", _must_not_run)
    monkeypatch.setattr(cli_module, "_build_llm_provider", lambda: MockLLMProvider())
    monkeypatch.setattr(
        cli_module,
        "resolve_llm_config",
        lambda: LLMConfig(
            provider="anthropic",
            protocol="anthropic",
            base_url="https://api.anthropic.com",
            api_key="test-key",
            model="test-model",
        ),
    )
    monkeypatch.setattr(cli_module, "SMTPSender", lambda: _SpySender())

    async def _fake_run_once(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(cli_module, "run_once", _fake_run_once)

    with caplog.at_level("WARNING", logger=cli_module.__name__):
        await cli_module.process_main_async()

    assert any(
        "follow-up monitoring skipped" in record.message and "scan-inbox" in record.message
        for record in caplog.records
    )


# --- jurisdiction detection (issue #63): a proposal, never an assignment --------------------
#
# No real WebSearchProvider/LLMProvider is used anywhere below -- a fake detector stands in,
# following this repo's established fake-collaborator pattern (see `_FakeDiscoverySource`/
# `_SpySender` above).


def _search_result(url: str = "https://example.com/privacy") -> SearchResult:
    return SearchResult(title="Privacy Policy", url=url, snippet="Some evidence snippet.")


def test_search_results_detail_truncates_an_oversized_snippet() -> None:
    """`_search_results_detail()` must cap `snippet` itself, not assume `SearchResult`s reaching it
    are already bounded -- `Attempt.results` carries the *raw* results from `WebSearchProvider`,
    not the separately-truncated form each detector module's own `_user_prompt()` builds for the
    LLM call. Without this cap, an oversized (or adversarial, on a hostile site's own indexed page)
    `content` field from the search backend would reach `Event.detail` (a JSON column with no size
    limit) and render untruncated on the case-detail page."""
    oversized = "x" * 10_000
    result = SearchResult(title="t", url="https://example.com", snippet=oversized)

    detail = cli_module._search_results_detail((result,))

    assert len(detail[0]["snippet"]) == cli_module._LOGGED_SNIPPET_MAX_CHARS
    assert detail[0]["snippet"] == oversized[: cli_module._LOGGED_SNIPPET_MAX_CHARS]


class _FakeJurisdictionDetector:
    """Duck-types `JurisdictionDetector.propose()` -- returns a fixed `JurisdictionAttempt`, or
    raises to simulate a failed/unavailable web-search or LLM backend."""

    def __init__(
        self, proposal: JurisdictionProposal | None = None, *, raise_error: bool = False
    ) -> None:
        self._proposal = proposal
        self._raise_error = raise_error
        self.calls: list[tuple[str, str]] = []

    async def propose(self, *, site: str, domain: str) -> JurisdictionAttempt:
        self.calls.append((site, domain))
        if self._raise_error:
            raise RuntimeError("web-search backend unreachable")
        return JurisdictionAttempt(
            query=f"{site} {domain} jurisdiction query",
            results=(_search_result(),) if self._proposal is not None else (),
            proposal=self._proposal,
        )


async def test_jurisdiction_detection_failure_falls_back_to_manual_prompt_unchanged(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """A detector whose underlying web-search/LLM call fails must not crash `run_once()` -- the
    case falls through to exactly issue #43's original manual-prompt-with-global-default
    behavior, with no `jurisdiction_proposed` Event recorded, but a `jurisdiction_detection_failed`
    Event recorded instead so the failure is on the case's own audit trail, not just the console."""
    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    llm = MockLLMProvider()
    detector = _FakeJurisdictionDetector(raise_error=True)
    # Blank answers for jurisdiction/language (issue #43's original "defer to global default"
    # behavior) -- nothing about this differs from a run with no detector; recipient confirmation
    # and send approval are no longer solicited live inside run_once() at all.
    io = _FakeIO(answers=["", ""])
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        jurisdiction_detector=detector,
        model="test-model",
    )

    case = session.exec(select(Case)).one()
    # Blank input left the per-case field unset at prompt time -- `_classify()` (issue #43's
    # existing resolution, unrelated to detection) later writes the resolved global default back
    # onto the Case once it advances past `Discovered`, exactly as it would with no detector at
    # all -- proving detection's failure changed nothing about that pre-existing behavior.
    assert case.applicable_jurisdiction == global_config.default_applicable_jurisdiction
    assert detector.calls == [("Example", "example.com")]  # detection was attempted...
    events = session.exec(select(Event).where(Event.case_id == case.id)).all()
    kinds = [e.kind for e in events]
    assert "jurisdiction_proposed" not in kinds  # ...but produced no proposal/Event on failure.
    # Real report: a silent fallback (bare manual prompt, no explanation) was indistinguishable
    # from detection never having been attempted -- the failure must be reported, both on the
    # console and on the case's own audit trail.
    assert any(
        "jurisdiction research for example.com failed" in line.lower() for line in io.printed
    )
    failed_events = [e for e in events if e.kind == "jurisdiction_detection_failed"]
    assert len(failed_events) == 1
    assert "web-search backend unreachable" in failed_events[0].detail["error"]


async def test_jurisdiction_detection_no_confident_proposal_is_reported_too(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """The non-error "couldn't determine a jurisdiction" outcome (detector returns `None` without
    raising) gets the same reporting treatment as a real failure -- an operator should never see a
    bare manual prompt with no idea whether detection ran at all."""
    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    llm = MockLLMProvider()
    detector = _FakeJurisdictionDetector(proposal=None)  # no raise -- a genuine low-confidence None
    io = _FakeIO(answers=["", ""])
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        jurisdiction_detector=detector,
        model="test-model",
    )

    assert any(
        "no confident jurisdiction suggestion found for example.com" in line.lower()
        for line in io.printed
    )
    case = session.exec(select(Case)).one()
    events = session.exec(select(Event).where(Event.case_id == case.id)).all()
    no_proposal_events = [e for e in events if e.kind == "jurisdiction_no_proposal"]
    assert len(no_proposal_events) == 1
    assert no_proposal_events[0].detail["query"] == "Example example.com jurisdiction query"
    assert no_proposal_events[0].detail["results"] == []


async def test_jurisdiction_proposal_reasoning_recorded_as_event(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """A successful proposal is recorded as a `jurisdiction_proposed` Event carrying the LLM's
    reasoning and sources -- auditable, not just the final accepted/overridden value."""
    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    llm = MockLLMProvider()
    proposal = JurisdictionProposal(
        jurisdiction="GDPR",
        reasoning="The privacy policy names an EU-based data controller.",
        sources=("https://example.com/privacy",),
    )
    detector = _FakeJurisdictionDetector(proposal=proposal)
    # Accept the suggestion (blank), blank for language.
    io = _FakeIO(answers=["", ""])
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        jurisdiction_detector=detector,
        model="test-model",
    )

    case = session.exec(select(Case)).one()
    assert case.applicable_jurisdiction == "GDPR"  # the human accepted the suggested default

    events = session.exec(select(Event).where(Event.case_id == case.id)).all()
    proposed_events = [e for e in events if e.kind == "jurisdiction_proposed"]
    assert len(proposed_events) == 1
    detail = proposed_events[0].detail
    assert detail["jurisdiction"] == "GDPR"
    assert "EU-based data controller" in detail["reasoning"]
    assert detail["sources"] == ["https://example.com/privacy"]
    assert detail["query"] == "Example example.com jurisdiction query"
    assert detail["results"] == [
        {
            "title": "Privacy Policy",
            "url": "https://example.com/privacy",
            "snippet": "Some evidence snippet.",
        }
    ]
    assert proposed_events[0].actor == "agent"


async def test_run_once_without_a_jurisdiction_detector_behaves_exactly_as_before(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """Omitting `jurisdiction_detector` entirely (its default) must reproduce issue #43's original
    behavior byte-for-byte -- this is what every pre-existing caller/test in this file already
    exercises without passing it, so this test just makes the "additive, opt-in only" guarantee
    explicit for issue #63."""
    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    llm = MockLLMProvider()
    io = _FakeIO(answers=["", ""])
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
    )

    case = session.exec(select(Case)).one()
    assert case.applicable_jurisdiction == global_config.default_applicable_jurisdiction
    events = session.exec(select(Event).where(Event.case_id == case.id)).all()
    assert all(e.kind != "jurisdiction_proposed" for e in events)


# --- recipient detection (issue #64): an unverified proposal, never an assignment -----------
#
# No real WebSearchProvider/LLMProvider is used anywhere below -- a fake detector stands in,
# following this repo's established fake-collaborator pattern.


class _FakeRecipientDetector:
    """Duck-types `RecipientDetector.propose()` -- returns a fixed `RecipientAttempt`, or raises to
    simulate a failed/unavailable web-search or LLM backend."""

    def __init__(
        self, proposal: RecipientProposal | None = None, *, raise_error: bool = False
    ) -> None:
        self._proposal = proposal
        self._raise_error = raise_error
        self.calls: list[tuple[str, str]] = []

    async def propose(self, *, site: str, domain: str) -> RecipientAttempt:
        self.calls.append((site, domain))
        if self._raise_error:
            raise RuntimeError("web-search backend unreachable")
        return RecipientAttempt(
            query=f"{site} {domain} recipient query",
            results=(_search_result(),) if self._proposal is not None else (),
            proposal=self._proposal,
        )


# `prompt_recipient_email()` was deleted in the automated-case-loop rework -- a Case reaching
# `Draft ready` with no `recipient_email` is simply left there; `case_actions.confirm_recipient()`
# (kanban or the `confirm-recipient` CLI subcommand) is the only way to set one now, and that
# function is already directly unit-tested in `tests/test_case_actions.py`/
# `tests/test_cli_case_actions.py`. The "a proposal is a suggestion, never an assignment" property
# this file used to prove via `prompt_recipient_email()`'s 'y'/typed-override branches now holds
# structurally instead: nothing but that explicit, separate call can ever write
# `Case.recipient_email`, which `test_recipient_proposal_reasoning_and_sources_recorded_as_event`
# below still demonstrates at the `run_once()` orchestration level (a proposal is recorded as an
# Event and the field stays untouched). The visually-distinguishable-unverified-proposal test
# (`test_unverified_recipient_proposal_is_visually_distinguishable_from_a_confirmed_value`) was
# also deleted outright -- it exercised `prompt_recipient_email()`'s console rendering directly,
# which no longer exists; an equivalent rendering concern for the kanban UI belongs to `api/`'s own
# tests, not this module's.


async def test_recipient_detection_failure_falls_back_to_manual_entry_unchanged(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """A detector whose underlying web-search/LLM call fails must not crash `run_once()` -- the
    case is simply left at `Draft ready` with no recipient (exactly as it would be with no detector
    at all), no `recipient_proposed` Event recorded, but a `recipient_detection_failed` Event
    recorded instead so the failure is on the case's own audit trail."""
    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    llm = MockLLMProvider()
    detector = _FakeRecipientDetector(raise_error=True)
    io = _FakeIO(answers=["", ""])  # jurisdiction (blank=default), language (blank=default)
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        recipient_detector=detector,
        model="test-model",
    )

    case = session.exec(select(Case)).one()
    assert case.state == CaseState.DRAFT_READY
    assert case.recipient_email is None
    assert sender.sent == []
    assert detector.calls == [("Example", "example.com")]  # detection was attempted...
    events = session.exec(select(Event).where(Event.case_id == case.id)).all()
    kinds = [e.kind for e in events]
    assert "recipient_proposed" not in kinds  # ...but produced no proposal/Event on failure.
    # Real report: a silent fallback (bare manual prompt, no explanation) was indistinguishable
    # from detection never having been attempted -- the failure must be reported, both on the
    # console and on the case's own audit trail.
    assert any("recipient research for example.com failed" in line.lower() for line in io.printed)
    failed_events = [e for e in events if e.kind == "recipient_detection_failed"]
    assert len(failed_events) == 1
    assert "web-search backend unreachable" in failed_events[0].detail["error"]

    # A human confirming the recipient (via case_actions/CLI) manually and approving is what
    # actually sends -- the failed detection changed nothing about that.
    case_actions.confirm_recipient(session, case.id, "privacy@manually-entered.example")
    case_actions.approve_draft(session, case.id)
    session.commit()
    await run_once(
        session,
        None,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
    )
    session.refresh(case)
    assert case.state == CaseState.AWAITING_RESPONSE
    assert sender.sent[0].to_address == "privacy@manually-entered.example"


async def test_recipient_detection_no_confident_proposal_is_reported_too(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """The non-error "couldn't determine a recipient" outcome (detector returns `None` without
    raising) gets the same reporting treatment as a real failure -- the exact scenario a real
    operator hit: SearXNG was reachable and returning results, but detection still produced no
    suggestion, and the silence made it impossible to tell whether detection had even run."""
    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    llm = MockLLMProvider()
    detector = _FakeRecipientDetector(proposal=None)  # no raise -- a genuine low-confidence None
    io = _FakeIO(answers=["", ""])
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        recipient_detector=detector,
        model="test-model",
    )

    assert any(
        "no confident recipient suggestion found for example.com" in line.lower()
        for line in io.printed
    )
    case = session.exec(select(Case)).one()
    events = session.exec(select(Event).where(Event.case_id == case.id)).all()
    no_proposal_events = [e for e in events if e.kind == "recipient_no_proposal"]
    assert len(no_proposal_events) == 1
    assert no_proposal_events[0].detail["query"] == "Example example.com recipient query"
    assert no_proposal_events[0].detail["results"] == []


async def test_recipient_proposal_reasoning_and_sources_recorded_as_event(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """A successful proposal is recorded as a `recipient_proposed` Event carrying the LLM's
    reasoning and sources -- auditable -- while `Case.recipient_email` itself is left untouched
    until a human explicitly confirms it via `case_actions.confirm_recipient()` (what the agent
    suggested is worth keeping on the audit trail, for a value a wrong guess could misdirect a real
    request to)."""
    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    llm = MockLLMProvider()
    proposal = RecipientProposal(
        recipient="dpo@example.com",
        reasoning="The privacy policy names dpo@example.com for data-deletion requests.",
        sources=("https://example.com/privacy", "https://example.com/legal"),
    )
    detector = _FakeRecipientDetector(proposal=proposal)
    io = _FakeIO(answers=["", ""])  # jurisdiction (blank), language (blank)
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        recipient_detector=detector,
        model="test-model",
    )

    case = session.exec(select(Case)).one()
    # The proposal never wrote to the Case on its own -- only an explicit confirm_recipient() call
    # could ever do that (issue #64).
    assert case.recipient_email is None

    events = session.exec(select(Event).where(Event.case_id == case.id)).all()
    proposed_events = [e for e in events if e.kind == "recipient_proposed"]
    assert len(proposed_events) == 1
    detail = proposed_events[0].detail
    assert detail["recipient"] == "dpo@example.com"
    assert "data-deletion requests" in detail["reasoning"]
    assert detail["sources"] == ["https://example.com/privacy", "https://example.com/legal"]
    assert detail["query"] == "Example example.com recipient query"
    assert detail["results"] == [
        {
            "title": "Privacy Policy",
            "url": "https://example.com/privacy",
            "snippet": "Some evidence snippet.",
        }
    ]
    assert proposed_events[0].actor == "agent"

    # A human confirming the suggested address is what actually sets it.
    case_actions.confirm_recipient(session, case.id, "dpo@example.com")
    session.commit()
    session.refresh(case)
    assert case.recipient_email == "dpo@example.com"


async def test_run_once_without_a_recipient_detector_behaves_exactly_as_before(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """Omitting `recipient_detector` entirely (its default) must reproduce issue #52's original
    "recipient is never inferred" guarantee byte-for-byte -- no suggestion shown, and no
    `recipient_proposed` Event, leaving `Case.recipient_email` unset exactly as with a detector that
    found nothing. This makes the "additive, opt-in only" guarantee explicit for issue #64."""
    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    llm = MockLLMProvider()
    io = _FakeIO(answers=["", ""])
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
    )

    case = session.exec(select(Case)).one()
    assert case.state == CaseState.DRAFT_READY
    assert case.recipient_email is None
    events = session.exec(select(Event).where(Event.case_id == case.id)).all()
    assert all(e.kind != "recipient_proposed" for e in events)


def test_build_recipient_detector_still_builds_a_detector_when_no_websearch_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no SearXNG/web-search config, `_build_recipient_detector()` still returns a working
    detector (`websearch=None`) rather than `None` -- crawling the site itself needs no search
    backend at all, so detection is no longer gated on one being configured (real report: a user
    who never set up SearXNG still wants the agent to navigate the site it was given a URL for)."""

    def _raise_lookup() -> None:
        raise LookupError("no web-search backend configured")

    monkeypatch.setattr(cli_module, "resolve_searxng_config", lambda: _raise_lookup())
    monkeypatch.setattr(
        cli_module,
        "resolve_llm_config",
        lambda: LLMConfig(
            provider="anthropic",
            protocol="anthropic",
            base_url="https://api.anthropic.com",
            api_key="test-key",
            model="test-model",
        ),
    )

    detector = cli_module._build_recipient_detector(MockLLMProvider())

    assert detector is not None
    assert detector._websearch is None  # noqa: SLF001


def test_build_jurisdiction_detector_wires_an_http_webfetch_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once web search is configured at all, page fetching is wired in unconditionally (no
    separate credential/instance to set up, unlike SearXNG itself -- see
    `_build_jurisdiction_detector()`'s docstring) -- real report: a search snippet alone can miss
    information (e.g. a contact email) that only lives in the page body."""
    from rtbf_agent.plugins.webfetch.http import HTTPWebFetchProvider
    from rtbf_agent.plugins.websearch.config import SearXNGConfig

    monkeypatch.setattr(
        cli_module,
        "resolve_searxng_config",
        lambda: SearXNGConfig(base_url="http://localhost:8080"),
    )
    monkeypatch.setattr(
        cli_module,
        "resolve_llm_config",
        lambda: LLMConfig(
            provider="anthropic",
            protocol="anthropic",
            base_url="https://api.anthropic.com",
            api_key="test-key",
            model="test-model",
        ),
    )

    detector = cli_module._build_jurisdiction_detector(MockLLMProvider())

    assert detector is not None
    assert isinstance(detector._webfetch, HTTPWebFetchProvider)  # noqa: SLF001


def test_build_recipient_detector_wires_an_http_webfetch_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sibling of the jurisdiction-detector version above."""
    from rtbf_agent.plugins.webfetch.http import HTTPWebFetchProvider
    from rtbf_agent.plugins.websearch.config import SearXNGConfig

    monkeypatch.setattr(
        cli_module,
        "resolve_searxng_config",
        lambda: SearXNGConfig(base_url="http://localhost:8080"),
    )
    monkeypatch.setattr(
        cli_module,
        "resolve_llm_config",
        lambda: LLMConfig(
            provider="anthropic",
            protocol="anthropic",
            base_url="https://api.anthropic.com",
            api_key="test-key",
            model="test-model",
        ),
    )

    detector = cli_module._build_recipient_detector(MockLLMProvider())

    assert detector is not None
    assert isinstance(detector._webfetch, HTTPWebFetchProvider)  # noqa: SLF001


# --- manual URL -> Case creation + registration cross-check (issue #66) ----------------------
#
# The second, human-initiated discovery path: instead of a whole-inbox Gmail scan, the operator
# pastes a URL for a site they already know about. No real Gmail account, OAuth token, or network
# call is used anywhere below -- the domain-scoped registration cross-check runs against an
# injected fake `DiscoverySource`, exactly as the whole-inbox path does in the tests above.


def _gmail_style_account(
    site: str, domain: str, target_email: str = "user@example.com"
) -> DiscoveredAccount:
    """A `DiscoveredAccount` shaped exactly as `GmailDiscoverySource.discover()` produces one --
    carrying `email_id`/`matched_pattern`/`matched_header` evidence, not manual-URL evidence."""
    return DiscoveredAccount(
        site=site,
        domain=domain,
        target_email=target_email,
        evidence={
            "email_id": f"msg-{domain}",
            "matched_pattern": "welcome_email",
            "matched_header": "Subject",
        },
    )


def test_extract_domain_handles_bare_domains_and_full_urls() -> None:
    """A pasted URL is reduced to its bare host via `urlsplit`/`.hostname` (never a regex):
    scheme, path, query, fragment, userinfo, port, casing, and a leading `www.` are all stripped,
    and a bare domain is handled identically to a full URL."""
    assert extract_domain("https://example.com/account/settings?tab=privacy") == "example.com"
    assert extract_domain("example.com") == "example.com"
    assert extract_domain("example.com/signup") == "example.com"
    assert extract_domain("HTTPS://User:pw@Example.COM:8443/x#frag") == "example.com"
    # A leading www. is dropped so a pasted www URL dedupes against a Gmail-discovered bare domain.
    assert extract_domain("https://www.example.com") == "example.com"
    for bad in ["", "   ", "https://"]:
        with pytest.raises(ValueError):
            extract_domain(bad)


async def test_url_creates_case_with_correct_domain_and_dedup_key(session: Session) -> None:
    """Pasting a URL creates one `Case` for its domain and the configured target email, tagged with
    manual-URL evidence -- and a second paste of the same site (different path) is deduped to that
    same `Case` on the shared `(domain, target_email)` key, never creating a duplicate."""
    case = await add_case_for_url(session, "https://example.com/privacy", "user@example.com")

    assert case.domain == "example.com"
    assert case.target_email == "user@example.com"
    assert case.state == CaseState.DISCOVERED
    assert case.id is not None
    evidence = json.loads(case.discovery_evidence)
    assert evidence["source"] == "manual_url"
    assert evidence["url"] == "https://example.com/privacy"

    # A second paste of the same site (different URL path) must resolve to the same Case, not a dup.
    again = await add_case_for_url(session, "http://example.com/account", "user@example.com")
    assert again.id == case.id
    assert len(session.exec(select(Case)).all()) == 1


async def test_registration_cross_check_finds_existing_gmail_evidence_for_the_domain(
    session: Session,
) -> None:
    """When the domain-scoped Gmail cross-check finds matching signup/confirmation mail, that
    evidence is merged into `Case.discovery_evidence` (queryable, not just printed) alongside the
    manual-URL evidence -- and the pre-existing manual-URL evidence is preserved, not replaced."""
    # A fake stands in for a `GmailDiscoverySource` scoped to `from:example.com`: it returns
    # exactly what that source's unchanged subject-pattern matching would yield for this domain.
    registration_source = _FakeDiscoverySource([_gmail_style_account("Example", "example.com")])

    case = await add_case_for_url(
        session,
        "https://example.com",
        "user@example.com",
        registration_check_source=registration_source,
    )

    evidence = json.loads(case.discovery_evidence)
    # The original manual-URL evidence is preserved...
    assert evidence["source"] == "manual_url"
    # ...and the cross-check outcome is recorded, carrying the Gmail-discovered evidence.
    check = evidence[REGISTRATION_CHECK_KEY]
    assert check["status"] == "evidence_found"
    assert check["matches"][0]["email_id"] == "msg-example.com"
    assert check["matches"][0]["matched_pattern"] == "welcome_email"


async def test_registration_cross_check_records_honest_absence_when_no_evidence_found(
    session: Session,
) -> None:
    """R3: a "nothing found" outcome is recorded explicitly onto `Case.discovery_evidence`, never
    silently dropped. An empty domain-scoped cross-check leaves a clear sentinel, not an absent
    key that would be indistinguishable from "the cross-check never ran"."""
    registration_source = _FakeDiscoverySource([])  # inbox holds no matching mail for this domain

    case = await add_case_for_url(
        session,
        "https://example.com",
        "user@example.com",
        registration_check_source=registration_source,
    )

    evidence = json.loads(case.discovery_evidence)
    assert evidence["source"] == "manual_url"
    assert evidence[REGISTRATION_CHECK_KEY] == "no matching email found in inbox"


async def test_cross_check_ignores_other_domains_the_source_returns(session: Session) -> None:
    """The cross-check filters results to the pasted domain, so even if the source returned mail
    from another sender it could never attach to this Case (recorded as an honest absence)."""
    registration_source = _FakeDiscoverySource([_gmail_style_account("Other", "other.com")])

    case = await add_case_for_url(
        session,
        "https://example.com",
        "user@example.com",
        registration_check_source=registration_source,
    )

    evidence = json.loads(case.discovery_evidence)
    assert evidence[REGISTRATION_CHECK_KEY] == "no matching email found in inbox"


async def test_cross_check_failure_never_crashes_creation_and_is_recorded_distinctly(
    session: Session,
) -> None:
    """A Gmail transport/auth error during the (best-effort, informational) cross-check must never
    crash the add-case operation -- the `Case` is already committed -- and must be recorded as a
    *distinct* "could not be completed" outcome, never the "nothing found" sentinel (which would
    falsely imply a confident negative, undermining R3's honest-absence guarantee)."""

    class _RaisingDiscoverySource(DiscoverySource):
        async def discover(self, target_email: str) -> Sequence[DiscoveredAccount]:
            raise RuntimeError("Gmail API unreachable")

    case = await add_case_for_url(
        session,
        "https://example.com",
        "user@example.com",
        registration_check_source=_RaisingDiscoverySource(),
    )

    # Creation succeeded despite the cross-check error.
    assert case.id is not None
    assert case.state == CaseState.DISCOVERED
    evidence = json.loads(case.discovery_evidence)
    assert evidence["source"] == "manual_url"  # manual-URL evidence preserved
    outcome = evidence[REGISTRATION_CHECK_KEY]
    # Recorded as a failure, NOT as a confident "nothing found".
    assert outcome != "no matching email found in inbox"
    assert "could not be completed" in outcome


async def test_cross_check_matches_a_sending_subdomain_but_not_a_look_alike(
    session: Session,
) -> None:
    """Signup mail commonly comes from a sending subdomain (`noreply@e.example.com`), which Gmail's
    `from:example.com` query returns -- the cross-check must count that as evidence for
    `example.com`, not a false absence. But a `.`-anchored suffix check must never let a look-alike
    (`notexample.com`, `example.com.evil.com`) through, so Gmail's loose substring matching can't
    smuggle in a foreign sender."""
    registration_source = _FakeDiscoverySource(
        [
            _gmail_style_account("Example Mail", "e.example.com"),  # legit sending subdomain
            _gmail_style_account("Look-alike", "notexample.com"),  # must NOT match
            _gmail_style_account("Suffix trick", "example.com.evil.com"),  # must NOT match
        ]
    )

    case = await add_case_for_url(
        session,
        "https://example.com",
        "user@example.com",
        registration_check_source=registration_source,
    )

    check = json.loads(case.discovery_evidence)[REGISTRATION_CHECK_KEY]
    assert check["status"] == "evidence_found"
    matched_ids = {m["email_id"] for m in check["matches"]}
    assert matched_ids == {"msg-e.example.com"}  # only the real subdomain, neither look-alike


async def test_manually_created_case_is_deduped_against_a_later_gmail_scan_discovery(
    session: Session,
) -> None:
    """Both directions of the shared `(domain, target_email)` dedup key, proving the manual path
    and the Gmail-scan path can never double-create a Case for the same site.

    Direction 1: a manual add first, then a Gmail scan surfaces the same domain -> no duplicate.
    Direction 2 (the inverse): a Gmail scan first, then a manual add for the same domain -> no
    duplicate either."""
    # --- Direction 1: manual add, then Gmail scan ---------------------------------------------
    manual_case = await add_case_for_url(session, "https://example.com", "user@example.com")

    # A later Gmail scan surfaces the same site (as GmailDiscoverySource would) -- must not dup.
    gmail_created = create_cases_for_new_accounts(
        session, [_gmail_style_account("Example", "example.com")]
    )
    session.commit()
    assert gmail_created == []  # the Gmail discovery deduped against the manually-created Case
    example_cases = session.exec(select(Case).where(Case.domain == "example.com")).all()
    assert len(example_cases) == 1
    assert example_cases[0].id == manual_case.id

    # --- Direction 2 (inverse): Gmail scan first, then manual add ------------------------------
    gmail_first = create_cases_for_new_accounts(
        session, [_gmail_style_account("Other Site", "other.com")]
    )
    session.commit()
    assert [c.domain for c in gmail_first] == ["other.com"]

    manual_again = await add_case_for_url(session, "https://other.com/privacy", "user@example.com")
    assert manual_again.id == gmail_first[0].id  # manual add deduped against the Gmail-scan Case
    other_cases = session.exec(select(Case).where(Case.domain == "other.com")).all()
    assert len(other_cases) == 1


async def test_add_case_for_url_auto_applies_jurisdiction_for_a_genuinely_new_case(
    session: Session,
) -> None:
    """Issue #63 parity follow-up (no-prompt reversal): a case created via `add_case_for_url()`
    used to get no jurisdiction detection at all -- with `jurisdiction_detector` given, it now gets
    exactly what `run_once()`'s per-created-case loop already gives a Gmail-discovered case: a
    jurisdiction proposal (recorded as a `jurisdiction_proposed` Event) auto-applied directly onto
    `Case.applicable_jurisdiction`, with no prompt of any kind."""
    proposal = JurisdictionProposal(
        jurisdiction="GDPR",
        reasoning="The privacy policy names an EU-based data controller.",
        sources=("https://example.com/privacy",),
    )
    detector = _FakeJurisdictionDetector(proposal=proposal)
    io = _FakeIO(answers=[])  # no prompt should ever consume an answer

    case = await add_case_for_url(
        session,
        "https://example.com/privacy",
        "user@example.com",
        jurisdiction_detector=detector,
        print_fn=io.print_fn,
    )

    assert detector.calls == [("example.com", "example.com")]
    assert case.applicable_jurisdiction == "GDPR"  # auto-applied, no human confirmation needed
    events = session.exec(select(Event).where(Event.case_id == case.id)).all()
    assert any(e.kind == "jurisdiction_proposed" for e in events)


async def test_add_case_for_url_dedup_hit_is_never_re_detected_even_if_detector_given(
    session: Session,
) -> None:
    """A dedup hit onto a pre-existing Case must not trigger detection/auto-apply even when a
    detector is supplied -- detection is meant to run exactly once, right after creation, mirroring
    `run_once()`'s "only genuinely new Cases" rule."""
    detector = _FakeJurisdictionDetector(
        proposal=JurisdictionProposal(jurisdiction="GDPR", reasoning="x", sources=())
    )

    first = await add_case_for_url(session, "https://example.com/privacy", "user@example.com")
    assert first.applicable_jurisdiction is None

    again = await add_case_for_url(
        session,
        "http://example.com/account",
        "user@example.com",
        jurisdiction_detector=detector,
    )

    assert again.id == first.id
    assert detector.calls == []  # detection never even ran for the dedup hit
    assert again.applicable_jurisdiction is None  # never detected, never written


async def test_add_case_for_url_without_new_params_is_unchanged(session: Session) -> None:
    """Omitting `global_config`/`jurisdiction_detector` (every pre-existing caller) must reproduce
    the original behavior byte-for-byte: no detection, no prompt, `applicable_jurisdiction` stays
    `None`."""
    case = await add_case_for_url(session, "https://example.com/privacy", "user@example.com")

    assert case.applicable_jurisdiction is None
    assert case.drafting_language is None
    events = session.exec(select(Event).where(Event.case_id == case.id)).all()
    assert events == []


def test_registration_check_source_is_scoped_to_the_pasted_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_build_gmail_registration_check_source()` reuses the same `GmailDiscoverySource` the whole-
    inbox scan uses, only narrowed to the pasted domain via a `from:<domain>` Gmail search query --
    no new site-specific probing logic, just a differently-scoped query. Proven by capturing the
    `query` handed to the shared source builder, with no real OAuth/Gmail call anywhere."""
    captured: dict[str, object] = {}
    sentinel = object()

    def _fake_build(query: str | None = None) -> object:
        captured["query"] = query
        return sentinel

    monkeypatch.setattr(cli_module, "_build_gmail_discovery_source", _fake_build)

    result = cli_module._build_gmail_registration_check_source("example.com")

    assert result is sentinel
    assert captured["query"] == "from:example.com"


def test_main_always_opens_the_interactive_shell_regardless_of_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Since the "shell is the only CLI surface" rework, `main()` has exactly one behavior: open
    the interactive shell, unconditionally. There is no subcommand dispatch left to test -- an
    empty argv, a former subcommand name, or anything else all do the same thing (see
    `main()`'s docstring for why `argv` is accepted but otherwise ignored)."""
    ran: dict[str, bool] = {}

    def _fake_shell_main() -> None:
        ran["shell"] = True

    monkeypatch.setattr(cli_module, "shell_main", _fake_shell_main)

    for argv in ([], ["process"], ["scan-inbox"], ["--help"], None):
        ran.clear()
        cli_module.main(argv)
        assert ran == {"shell": True}


def _make_shell_engine():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    return engine


def _run_shell(monkeypatch: pytest.MonkeyPatch, engine, commands: list[str]) -> list[str]:
    """Drive `shell_main()` with a scripted list of input lines, returning everything printed."""
    monkeypatch.setattr(cli_module, "build_engine", lambda: engine)
    printed: list[str] = []
    commands_iter = iter(commands)

    def _fake_input(_prompt: str) -> str:
        try:
            return next(commands_iter)
        except StopIteration:
            raise EOFError from None

    cli_module.shell_main(input_fn=_fake_input, print_fn=printed.append)
    return printed


def _mock_add_case_collaborators(
    monkeypatch: pytest.MonkeyPatch, *, proposal: JurisdictionProposal | None = None
) -> _FakeJurisdictionDetector:
    """Mocks the three collaborators the shell's `add` command now builds per-invocation (issue
    #63 parity follow-up) -- `_build_llm_provider()`/`_build_jurisdiction_detector()`/
    `resolve_global_config()` -- with a `MockLLMProvider`/a `_FakeJurisdictionDetector` (returning
    `proposal`, `None` by default)/a fixed `GlobalConfig`, so a test can drive `add` without any
    real LLM/web-search backend. Returns the fake detector so a test can assert on `.calls`."""
    monkeypatch.setattr(cli_module, "_build_llm_provider", lambda: MockLLMProvider())
    detector = _FakeJurisdictionDetector(proposal=proposal)
    monkeypatch.setattr(cli_module, "_build_jurisdiction_detector", lambda llm: detector)
    monkeypatch.setattr(
        cli_module,
        "resolve_global_config",
        lambda: GlobalConfig(
            requester_name="Ada Lovelace",
            default_applicable_jurisdiction="GDPR",
            default_drafting_language="en",
        ),
    )
    return detector


def test_shell_help_command_lists_available_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    printed = _run_shell(monkeypatch, _make_shell_engine(), ["help", "exit"])
    assert any("add <url>" in line for line in printed)
    assert any("kanban" in line for line in printed)


def test_shell_list_command_reports_no_cases_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    printed = _run_shell(monkeypatch, _make_shell_engine(), ["list", "exit"])
    assert any("No cases yet" in line for line in printed)


def test_shell_add_command_creates_a_case_via_add_case_for_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RTBF_TARGET_EMAIL", "me@example.com")
    _mock_add_case_collaborators(monkeypatch)  # no proposal -- exercises the plain manual prompt
    engine = _make_shell_engine()

    # Two extra blank answers beyond "list"/"exit": the jurisdiction/drafting-language prompt
    # `add` now runs (issue #63 parity follow-up) shares the same input_fn as shell commands.
    printed = _run_shell(
        monkeypatch, engine, ["add https://example.com/privacy", "", "", "list", "exit"]
    )

    assert any("Created case for example.com" in line for line in printed)
    assert any("example.com" in line and "Discovered" in line for line in printed)


def test_shell_add_command_auto_applies_a_jurisdiction_proposal_with_no_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #63 parity follow-up (no-prompt reversal): the shell's `add` command now proposes and
    auto-applies a jurisdiction too, not just `add-case`/`scan-inbox` -- proving the fix reaches
    every case-creation entrypoint, with zero interactive prompt."""
    monkeypatch.setenv("RTBF_TARGET_EMAIL", "me@example.com")
    proposal = JurisdictionProposal(
        jurisdiction="GDPR", reasoning="EU-based data controller.", sources=()
    )
    detector = _mock_add_case_collaborators(monkeypatch, proposal=proposal)
    engine = _make_shell_engine()

    _run_shell(monkeypatch, engine, ["add https://example.com/privacy", "exit"])

    assert detector.calls == [("example.com", "example.com")]
    with Session(engine) as session:
        case = session.exec(select(Case)).one()
        assert case.applicable_jurisdiction == "GDPR"  # auto-applied, no prompt involved


def test_shell_add_command_reports_llm_provider_failure_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`add` now requires a configured LLM provider (parity with `add-case`/`scan-inbox`) -- a
    failure building one (e.g. `RTBF_LLM_PROVIDER` unset) must report cleanly via `error: ...` and
    leave the shell running, exactly like `draft-additional-info`'s identical failure-reporting
    shape, rather than crash the whole REPL."""
    monkeypatch.setenv("RTBF_TARGET_EMAIL", "me@example.com")

    def _raise() -> None:
        raise RuntimeError("RTBF_LLM_PROVIDER is not set")

    monkeypatch.setattr(cli_module, "_build_llm_provider", _raise)

    printed = _run_shell(
        monkeypatch, _make_shell_engine(), ["add https://example.com/privacy", "help", "exit"]
    )

    assert any("error: RTBF_LLM_PROVIDER is not set" in line for line in printed)
    assert any("add <url>" in line for line in printed)  # help still ran after the error


def test_shell_add_command_without_a_url_prints_usage_and_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed = _run_shell(monkeypatch, _make_shell_engine(), ["add", "exit"])
    assert any("usage: add <url>" in line for line in printed)


def test_shell_add_command_without_target_email_configured_reports_error_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing `RTBF_TARGET_EMAIL` must not crash the whole shell (OCR review finding on #83):
    `_resolve_target_email()`'s `LookupError` is caught and reported, and the shell keeps running
    to accept the next command."""
    monkeypatch.delenv("RTBF_TARGET_EMAIL", raising=False)
    printed = _run_shell(
        monkeypatch, _make_shell_engine(), ["add https://example.com", "help", "exit"]
    )
    assert any("error: no target email configured" in line for line in printed)
    assert any("add <url>" in line for line in printed)  # help still ran after the error


def test_shell_show_command_renders_case_and_event_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RTBF_TARGET_EMAIL", "me@example.com")
    _mock_add_case_collaborators(monkeypatch)
    engine = _make_shell_engine()

    _run_shell(monkeypatch, engine, ["add https://example.com/privacy", "", "", "exit"])
    with Session(engine) as session:
        case_id = session.exec(select(Case)).one().id

    printed = _run_shell(monkeypatch, engine, [f"show {case_id}", "exit"])
    rendered = "\n".join(printed)
    assert f"Case [{case_id}]" in rendered
    assert "example.com" in rendered
    assert "Events:" in rendered


def test_shell_show_command_reports_unknown_case_id(monkeypatch: pytest.MonkeyPatch) -> None:
    printed = _run_shell(monkeypatch, _make_shell_engine(), ["show 999", "exit"])
    assert any("No case with id 999" in line for line in printed)


def _make_shell_case(engine, **kwargs) -> int:
    with Session(engine) as session:
        case = Case(
            site="Example",
            domain="example.com",
            target_email="me@example.com",
            **kwargs,
        )
        session.add(case)
        session.commit()
        session.refresh(case)
        assert case.id is not None
        return case.id


def test_shell_confirm_recipient_command_records_the_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine, state=CaseState.DRAFT_READY)

    printed = _run_shell(
        monkeypatch, engine, [f"confirm-recipient {case_id} privacy@example.com", "exit"]
    )

    assert any("recipient confirmed" in line for line in printed)
    with Session(engine) as session:
        assert session.get(Case, case_id).recipient_email == "privacy@example.com"


def test_shell_confirm_recipient_command_reports_usage_for_missing_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed = _run_shell(monkeypatch, _make_shell_engine(), ["confirm-recipient 1", "exit"])
    assert any("usage: confirm-recipient" in line for line in printed)


def test_shell_confirm_recipient_command_reports_a_case_action_error_and_keeps_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed = _run_shell(
        monkeypatch, _make_shell_engine(), ["confirm-recipient 999 privacy@example.com", "help"]
    )
    assert any("error:" in line for line in printed)
    assert any("Available commands" in line for line in printed)


def test_shell_set_jurisdiction_command_records_the_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine)  # default state: Discovered

    printed = _run_shell(monkeypatch, engine, [f"set-jurisdiction {case_id} CCPA", "exit"])

    assert any("applicable jurisdiction set" in line for line in printed)
    with Session(engine) as session:
        assert session.get(Case, case_id).applicable_jurisdiction == "CCPA"


def test_shell_set_jurisdiction_command_reports_usage_for_missing_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed = _run_shell(monkeypatch, _make_shell_engine(), ["set-jurisdiction 1", "exit"])
    assert any("usage: set-jurisdiction" in line for line in printed)


def test_shell_set_jurisdiction_command_reports_a_case_action_error_and_keeps_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine, state=CaseState.DRAFT_READY)  # past Discovered
    printed = _run_shell(monkeypatch, engine, [f"set-jurisdiction {case_id} CCPA", "help"])
    assert any("error:" in line for line in printed)
    assert any("Available commands" in line for line in printed)


def test_shell_set_jurisdiction_command_reports_a_case_not_found_error_and_keeps_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed = _run_shell(monkeypatch, _make_shell_engine(), ["set-jurisdiction 999 CCPA", "help"])
    assert any("error:" in line for line in printed)
    assert any("Available commands" in line for line in printed)


def test_shell_set_drafting_language_command_records_the_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine)  # default state: Discovered

    printed = _run_shell(monkeypatch, engine, [f"set-drafting-language {case_id} fr", "exit"])

    assert any("drafting language set" in line for line in printed)
    with Session(engine) as session:
        assert session.get(Case, case_id).drafting_language == "fr"


def test_shell_set_drafting_language_command_reports_usage_for_missing_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed = _run_shell(monkeypatch, _make_shell_engine(), ["set-drafting-language 1", "exit"])
    assert any("usage: set-drafting-language" in line for line in printed)


def test_shell_set_drafting_language_command_reports_a_case_not_found_error_and_keeps_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed = _run_shell(
        monkeypatch, _make_shell_engine(), ["set-drafting-language 999 fr", "help"]
    )
    assert any("error:" in line for line in printed)
    assert any("Available commands" in line for line in printed)


def test_shell_approve_reject_draft_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_shell_engine()
    approve_id = _make_shell_case(engine, state=CaseState.DRAFT_READY)
    reject_id = _make_shell_case(engine, state=CaseState.DRAFT_READY)

    printed = _run_shell(
        monkeypatch,
        engine,
        [f"approve-draft {approve_id}", f"reject-draft {reject_id}", "exit"],
    )

    assert any("draft approved" in line for line in printed)
    assert any("draft rejected" in line for line in printed)
    with Session(engine) as session:
        assert session.get(Case, reject_id).state == CaseState.REJECTED_ESCALATED


def test_shell_approve_draft_command_reports_usage_for_a_non_numeric_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed = _run_shell(monkeypatch, _make_shell_engine(), ["approve-draft abc", "exit"])
    assert any("usage: approve-draft" in line for line in printed)


def _mock_llm_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mocks `cli_module._build_llm_provider()`/`resolve_llm_config()` with a `MockLLMProvider`/a
    fixed model name -- the established pattern this file already uses for every other LLM-backed
    CLI path (see e.g. the `serve`-command tests above), applied here for `draft-additional-info`,
    which now also needs a real (mocked) `LLMProvider` to compose a preview."""
    monkeypatch.setattr(cli_module, "_build_llm_provider", lambda: MockLLMProvider())
    monkeypatch.setattr(
        cli_module,
        "resolve_llm_config",
        lambda: LLMConfig(
            provider="anthropic",
            protocol="anthropic",
            base_url="https://api.anthropic.com",
            api_key="test-key",
            model="test-model",
        ),
    )


def _add_original_draft_and_request(engine, case_id: int) -> None:
    """A `draft_proposed` + `additional_info_requested` `Event` pair, as a real case in `Requires
    human action` always has -- `agent_loop.compose_additional_info_draft()`'s `latest_draft()`
    lookup (for the subject) needs the former to exist."""
    with Session(engine) as session:
        session.add(
            create_event(
                case_id=case_id,
                kind="draft_proposed",
                actor="agent",
                detail={"draft": "Please delete my data.", "subject": "Deletion request"},
            )
        )
        session.add(
            create_event(
                case_id=case_id,
                kind="additional_info_requested",
                actor="agent",
                detail={"reasoning": "site asked for date of birth"},
            )
        )
        session.commit()


def test_shell_draft_additional_info_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shell counterpart of the kanban's new "Draft" button (the drafting-is-a-preview
    redesign, superseding issue #117/#119's fused "this round is ready" draft+send action) --
    mirrors `test_shell_approve_reject_draft_commands`'s shape."""
    _mock_llm_config(monkeypatch)
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine, state=CaseState.REQUIRES_HUMAN_ACTION)
    _add_original_draft_and_request(engine, case_id)
    with Session(engine) as session:
        case_actions.provide_text_response(session, case_id, "my date of birth is 1990-01-01")

    printed = _run_shell(monkeypatch, engine, [f"draft-additional-info {case_id}", "exit"])

    assert any("draft composed" in line for line in printed)
    with Session(engine) as session:
        case = session.get(Case, case_id)
        assert case is not None
        assert case.state == CaseState.REQUIRES_HUMAN_ACTION  # drafting never moves case.state
        draft_events = list(
            session.exec(
                select(Event).where(Event.case_id == case_id, Event.kind == "draft_proposed")
            )
        )
        assert len(draft_events) == 2  # the original, plus this preview
        latest = max(draft_events, key=lambda event: event.id or 0)
        assert "1990-01-01" in latest.detail["draft"]


def test_shell_draft_additional_info_command_reports_usage_for_a_non_numeric_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed = _run_shell(monkeypatch, _make_shell_engine(), ["draft-additional-info abc", "exit"])
    assert any("usage: draft-additional-info" in line for line in printed)


def test_shell_draft_additional_info_command_reports_a_case_action_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing recorded yet for the case's current round -- `ValueError`, surfaced the same way any
    other error is in the shell."""
    _mock_llm_config(monkeypatch)
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine, state=CaseState.REQUIRES_HUMAN_ACTION)
    _add_original_draft_and_request(engine, case_id)

    printed = _run_shell(monkeypatch, engine, [f"draft-additional-info {case_id}", "help"])

    assert any("error:" in line for line in printed)
    assert any("Available commands" in line for line in printed)


def test_shell_confirm_dispute_followup_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """`confirm-followup` now only ever has a pending REJECTION to act on (maintainer-directed
    narrowing of issue #68: `follow_up()` resolves a CONFIRMATION classification itself -- see
    `followup.py`'s module docstring, "Why CONFIRMATION auto-closes now")."""
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine, state=CaseState.AWAITING_RESPONSE)
    with Session(engine) as session:
        session.add(create_event(case_id=case_id, kind="request_sent", actor="agent"))
        session.add(
            create_event(
                case_id=case_id,
                kind="response_classified",
                actor="agent",
                detail={"message_id": "m1", "intent": "rejection", "reasoning": "Refused."},
            )
        )
        session.commit()

    printed = _run_shell(monkeypatch, engine, [f"confirm-followup {case_id}", "exit"])

    assert any("follow-up confirmed" in line for line in printed)
    with Session(engine) as session:
        assert session.get(Case, case_id).state == CaseState.REJECTED_ESCALATED


def test_shell_dispute_followup_command(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine, state=CaseState.AWAITING_RESPONSE)
    with Session(engine) as session:
        session.add(create_event(case_id=case_id, kind="request_sent", actor="agent"))
        session.add(
            create_event(
                case_id=case_id,
                kind="response_classified",
                actor="agent",
                detail={"message_id": "m1", "intent": "rejection", "reasoning": "Refused."},
            )
        )
        session.commit()

    printed = _run_shell(monkeypatch, engine, [f"dispute-followup {case_id}", "exit"])

    assert any("follow-up disputed" in line for line in printed)
    with Session(engine) as session:
        assert session.get(Case, case_id).state == CaseState.REQUIRES_HUMAN_ACTION


def test_shell_reopen_case_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mitigating control for follow_up()'s CONFIRMATION auto-close: a human can undo a wrongly
    auto-closed case from the interactive shell."""
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine, state=CaseState.DELETION_CONFIRMED)

    printed = _run_shell(monkeypatch, engine, [f"reopen-case {case_id}", "exit"])

    assert any("reopened" in line for line in printed)
    with Session(engine) as session:
        assert session.get(Case, case_id).state == CaseState.REQUIRES_HUMAN_ACTION


def test_shell_reopen_case_wrong_state(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine, state=CaseState.AWAITING_RESPONSE)

    printed = _run_shell(monkeypatch, engine, [f"reopen-case {case_id}", "exit"])

    assert any("error:" in line for line in printed)


def test_shell_reopen_case_invalid_id(monkeypatch: pytest.MonkeyPatch) -> None:
    printed = _run_shell(monkeypatch, _make_shell_engine(), ["reopen-case abc", "exit"])
    assert any("usage: reopen-case" in line for line in printed)


def test_shell_provide_text_command_records_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine, state=CaseState.REQUIRES_HUMAN_ACTION)

    printed = _run_shell(
        monkeypatch, engine, [f"provide-text {case_id} my date of birth is 1990-01-01", "exit"]
    )

    assert any("text response recorded" in line for line in printed)
    with Session(engine) as session:
        events = session.exec(
            select(Event).where(
                Event.case_id == case_id, Event.kind == "additional_info_text_provided"
            )
        ).all()
    assert len(events) == 1
    assert events[0].detail["text"] == "my date of birth is 1990-01-01"


def test_shell_edit_draft_command_records_a_new_draft_proposed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine, state=CaseState.DRAFT_READY)
    with Session(engine) as session:
        session.add(
            create_event(
                case_id=case_id,
                kind="draft_proposed",
                actor="agent",
                detail={"draft": "original body", "subject": "original subject"},
            )
        )
        session.commit()

    printed = _run_shell(monkeypatch, engine, [f"edit-draft {case_id} hand-edited body", "exit"])

    assert any("draft updated" in line for line in printed)
    with Session(engine) as session:
        events = sorted(
            session.exec(
                select(Event).where(Event.case_id == case_id, Event.kind == "draft_proposed")
            ).all(),
            key=lambda event: event.id or 0,
        )
    assert len(events) == 2
    assert events[-1].detail["draft"] == "hand-edited body"
    assert events[-1].detail["subject"] == "original subject"


def test_shell_edit_draft_command_shows_usage_on_missing_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine, state=CaseState.DRAFT_READY)

    printed = _run_shell(monkeypatch, engine, [f"edit-draft {case_id}", "exit"])

    assert any("usage: edit-draft" in line for line in printed)


def test_shell_attach_document_command_records_the_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv(
        "RTBF_DOCUMENT_ENCRYPTION_KEY", "zJ8gk8h1qYw3F1v3l4v0m9C7b3e9r0g5t9p9nQe8s0A="
    )
    monkeypatch.setenv("RTBF_DOCUMENT_STORAGE_DIR", str(tmp_path / "documents"))
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine, state=CaseState.REQUIRES_HUMAN_ACTION)
    doc_path = tmp_path / "id.png"
    doc_path.write_bytes(b"fake image bytes")

    printed = _run_shell(monkeypatch, engine, [f"attach-document {case_id} {doc_path}", "exit"])

    assert any("document attached" in line for line in printed)


def test_shell_attach_document_command_accepts_a_quoted_path_with_spaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Real regression (OCR review): a `partition(" ")`-based parser truncated a path containing
    spaces at its first space instead of reading it as a single, shlex-quoted argument."""
    monkeypatch.setenv(
        "RTBF_DOCUMENT_ENCRYPTION_KEY", "zJ8gk8h1qYw3F1v3l4v0m9C7b3e9r0g5t9p9nQe8s0A="
    )
    monkeypatch.setenv("RTBF_DOCUMENT_STORAGE_DIR", str(tmp_path / "documents"))
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine, state=CaseState.REQUIRES_HUMAN_ACTION)
    doc_path = tmp_path / "my document.png"
    doc_path.write_bytes(b"fake image bytes")

    printed = _run_shell(monkeypatch, engine, [f'attach-document {case_id} "{doc_path}"', "exit"])

    assert any("document attached" in line for line in printed)


def test_shell_attach_document_command_reports_a_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine, state=CaseState.REQUIRES_HUMAN_ACTION)

    printed = _run_shell(
        monkeypatch, engine, [f"attach-document {case_id} {tmp_path / 'nope.png'}", "exit"]
    )

    assert any("error: no such file" in line for line in printed)


def test_shell_attach_document_command_accepts_a_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv(
        "RTBF_DOCUMENT_ENCRYPTION_KEY", "zJ8gk8h1qYw3F1v3l4v0m9C7b3e9r0g5t9p9nQe8s0A="
    )
    monkeypatch.setenv("RTBF_DOCUMENT_STORAGE_DIR", str(tmp_path / "documents"))
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine, state=CaseState.REQUIRES_HUMAN_ACTION)
    doc_path = tmp_path / "id.png"
    doc_path.write_bytes(b"fake image bytes")

    printed = _run_shell(
        monkeypatch, engine, [f"attach-document {case_id} {doc_path} ID card", "exit"]
    )

    assert any("document attached" in line for line in printed)
    with Session(engine) as session:
        document = session.exec(select(Document)).one()
        assert document.label == "ID card"


def test_shell_list_documents_command_prints_vault_contents(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv(
        "RTBF_DOCUMENT_ENCRYPTION_KEY", "zJ8gk8h1qYw3F1v3l4v0m9C7b3e9r0g5t9p9nQe8s0A="
    )
    monkeypatch.setenv("RTBF_DOCUMENT_STORAGE_DIR", str(tmp_path / "documents"))
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine, state=CaseState.REQUIRES_HUMAN_ACTION)
    doc_path = tmp_path / "id.png"
    doc_path.write_bytes(b"fake image bytes")

    printed = _run_shell(
        monkeypatch,
        engine,
        [f"attach-document {case_id} {doc_path} ID card", "list-documents", "exit"],
    )

    assert any("ID card" in line for line in printed)


def test_shell_select_documents_command_records_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv(
        "RTBF_DOCUMENT_ENCRYPTION_KEY", "zJ8gk8h1qYw3F1v3l4v0m9C7b3e9r0g5t9p9nQe8s0A="
    )
    monkeypatch.setenv("RTBF_DOCUMENT_STORAGE_DIR", str(tmp_path / "documents"))
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine, state=CaseState.REQUIRES_HUMAN_ACTION)
    doc_path = tmp_path / "id.png"
    doc_path.write_bytes(b"fake image bytes")
    _run_shell(monkeypatch, engine, [f"attach-document {case_id} {doc_path}", "exit"])
    with Session(engine) as session:
        document_id = session.exec(select(Document)).one().id

    printed = _run_shell(monkeypatch, engine, [f"select-documents {case_id} {document_id}", "exit"])

    assert any("1 document(s) selected" in line for line in printed)
    with Session(engine) as session:
        # attach_document() itself already records one documents_selected_for_resend event
        # (auto-selecting the freshly-uploaded document); select-documents records a second --
        # assert on the latest, not `.one()`.
        events = sorted(
            session.exec(
                select(Event).where(
                    Event.case_id == case_id, Event.kind == "documents_selected_for_resend"
                )
            ).all(),
            key=lambda event: event.id or 0,
        )
        assert events[-1].detail["document_ids"] == [document_id]


def test_shell_delete_document_command_removes_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv(
        "RTBF_DOCUMENT_ENCRYPTION_KEY", "zJ8gk8h1qYw3F1v3l4v0m9C7b3e9r0g5t9p9nQe8s0A="
    )
    monkeypatch.setenv("RTBF_DOCUMENT_STORAGE_DIR", str(tmp_path / "documents"))
    engine = _make_shell_engine()
    case_id = _make_shell_case(engine, state=CaseState.REQUIRES_HUMAN_ACTION)
    doc_path = tmp_path / "id.png"
    doc_path.write_bytes(b"fake image bytes")
    _run_shell(monkeypatch, engine, [f"attach-document {case_id} {doc_path}", "exit"])
    with Session(engine) as session:
        document_id = session.exec(select(Document)).one().id

    printed = _run_shell(monkeypatch, engine, [f"delete-document {document_id}", "exit"])

    assert any("deleted from the vault" in line for line in printed)
    with Session(engine) as session:
        document = session.get(Document, document_id)
        assert document.status == DocumentStatus.PURGED


def test_shell_serve_command_stops_on_keyboard_interrupt_and_returns_to_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raising_serve_main_async(interval_minutes, **_kwargs) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "serve_main_async", _raising_serve_main_async)

    printed = _run_shell(monkeypatch, _make_shell_engine(), ["serve", "help"])

    assert any("stopped" in line for line in printed)
    assert any("Available commands" in line for line in printed)


def test_shell_serve_command_passes_a_parsed_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_serve_main_async(interval_minutes, **_kwargs) -> None:
        captured["interval_minutes"] = interval_minutes

    monkeypatch.setattr(cli_module, "serve_main_async", _fake_serve_main_async)

    _run_shell(monkeypatch, _make_shell_engine(), ["serve 5", "exit"])

    assert captured["interval_minutes"] == 5.0


def test_shell_serve_command_reports_usage_for_a_non_numeric_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed = _run_shell(monkeypatch, _make_shell_engine(), ["serve abc", "exit"])
    assert any("usage: serve" in line for line in printed)


def test_shell_kanban_command_prints_start_instructions(monkeypatch: pytest.MonkeyPatch) -> None:
    printed = _run_shell(monkeypatch, _make_shell_engine(), ["kanban", "exit"])
    assert any("uvicorn rtbf_agent.api:app" in line for line in printed)


def test_shell_unknown_command_prints_hint_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed = _run_shell(monkeypatch, _make_shell_engine(), ["frobnicate", "exit"])
    assert any("unknown command: 'frobnicate'" in line for line in printed)


def test_shell_blank_lines_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    printed = _run_shell(monkeypatch, _make_shell_engine(), ["", "  ", "exit"])
    assert len(printed) == 1  # just the startup banner; blank lines produce no output


def test_shell_quit_alias_also_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    printed = _run_shell(monkeypatch, _make_shell_engine(), ["quit"])
    assert len(printed) == 1  # just the startup banner


def test_shell_eof_exits_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    printed = _run_shell(monkeypatch, _make_shell_engine(), [])
    assert printed[-1] == ""


def test_forget_console_script_is_registered() -> None:
    """`forget` (issue #82) is a real installed entrypoint alongside `rtbf-agent`, not just a
    manually-invoked function -- guards against the `[project.scripts]` entry silently drifting
    out of sync with `rtbf_agent.__main__:main`."""
    entry_points = importlib.metadata.entry_points(group="console_scripts")
    forget_entries = [ep for ep in entry_points if ep.name == "forget"]
    assert len(forget_entries) == 1
    assert forget_entries[0].value == "rtbf_agent.__main__:main"


async def test_run_once_with_no_discovery_source_skips_discovery_and_still_processes_existing_cases(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """Issue #86: a `Case` that already exists (e.g. added manually via `add-case`/the shell's
    `add`) must still be classified/drafted/sent by `run_once()` even with no discovery source at
    all -- the classify/draft/send pipeline is independent of Gmail discovery."""
    existing = Case(site="Example", domain="example.com", target_email="user@example.com")
    session.add(existing)
    session.commit()

    llm = MockLLMProvider()
    notifier = ConsoleNotifier()
    sender = _SpySender()

    await run_once(
        session,
        None,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        model="test-model",
    )
    session.refresh(existing)
    assert existing.state == CaseState.DRAFT_READY

    case_actions.confirm_recipient(session, existing.id, "privacy@example-corp.com")
    case_actions.approve_draft(session, existing.id)
    session.commit()

    await run_once(
        session,
        None,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        model="test-model",
    )

    session.refresh(existing)
    assert existing.state == CaseState.AWAITING_RESPONSE
    assert len(sender.sent) == 1


async def test_run_once_with_no_discovery_source_never_touches_the_discovery_source_argument(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """A second, stronger check on the same behavior: passing `None` (not merely a discovery
    source with nothing to return) is the actual contract -- nothing in `run_once()` should even
    reference the parameter beyond the `is not None` guard."""
    llm = MockLLMProvider()
    notifier = ConsoleNotifier()
    sender = _SpySender()

    await run_once(
        session,
        None,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        model="test-model",
    )

    assert session.exec(select(Case)).all() == []
    assert sender.sent == []


# --- serve (the recurring loop, automated-case-loop rework) -----------------------------------


def test_resolve_serve_interval_minutes_cli_flag_wins_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RTBF_SERVE_INTERVAL_MINUTES", "7")
    assert cli_module._resolve_serve_interval_minutes(3.0) == 3.0


def test_resolve_serve_interval_minutes_falls_back_to_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RTBF_SERVE_INTERVAL_MINUTES", "7")
    assert cli_module._resolve_serve_interval_minutes(None) == 7.0


def test_resolve_serve_interval_minutes_defaults_to_twenty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RTBF_SERVE_INTERVAL_MINUTES", raising=False)
    assert cli_module._resolve_serve_interval_minutes(None) == 20.0


@pytest.mark.parametrize("bad_value", ["not-a-number", "0", "-5"])
def test_resolve_serve_interval_minutes_rejects_a_bad_env_value(
    monkeypatch: pytest.MonkeyPatch, bad_value: str
) -> None:
    monkeypatch.setenv("RTBF_SERVE_INTERVAL_MINUTES", bad_value)
    with pytest.raises(ValueError, match="RTBF_SERVE_INTERVAL_MINUTES"):
        cli_module._resolve_serve_interval_minutes(None)


@pytest.mark.parametrize("bad_value", [0.0, -5.0])
def test_resolve_serve_interval_minutes_rejects_a_bad_cli_flag_value(bad_value: float) -> None:
    """OCR review finding: `--interval-minutes 0`/a negative value must be rejected with the same
    clear error `RTBF_SERVE_INTERVAL_MINUTES` already gets, not silently accepted into a busy loop
    (`0`) or left to `asyncio.sleep()` itself to raise a much less clear `ValueError` (negative)."""
    with pytest.raises(ValueError, match="--interval-minutes"):
        cli_module._resolve_serve_interval_minutes(bad_value)


class _StopServeLoop(Exception):
    """Breaks `serve_main_async()`'s `while True` loop deterministically in tests -- never raised
    in production code."""


async def test_serve_main_async_runs_run_once_fn_then_sleeps_for_the_resolved_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core loop shape: run a pass, then sleep for the resolved interval (in seconds), and
    repeat -- proven over a few bounded iterations rather than the real infinite loop, via the
    injected `run_once_fn`/`sleep_fn` (this function's own documented test-only escape hatch)."""
    monkeypatch.setattr(cli_module, "_resolve_notifier_backend", lambda: "console")
    calls: list[str] = []
    iterations = 3

    async def _fake_run_once() -> None:
        calls.append("run_once")

    async def _fake_sleep_and_stop(seconds: float) -> None:
        calls.append(f"sleep:{seconds}")
        if len([c for c in calls if c == "run_once"]) >= iterations:
            raise _StopServeLoop

    with pytest.raises(_StopServeLoop):
        await cli_module.serve_main_async(
            2.0,
            print_fn=lambda _text: None,
            sleep_fn=_fake_sleep_and_stop,
            run_once_fn=_fake_run_once,
        )

    assert calls == [
        "run_once",
        "sleep:120.0",
        "run_once",
        "sleep:120.0",
        "run_once",
        "sleep:120.0",
    ]


async def test_serve_main_async_continues_past_a_failing_pass_and_notifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9/robustness: one pass raising must not kill the loop or go unreported -- it's logged,
    printed, and sent through the (independently-built) notifier, then the loop continues to the
    next interval exactly as if nothing had happened."""
    monkeypatch.setattr(cli_module, "_resolve_notifier_backend", lambda: "console")
    notifications: list[str] = []

    class _RecordingConsoleNotifier(ConsoleNotifier):
        async def notify(self, message) -> None:  # type: ignore[override]
            notifications.append(message.text)

    monkeypatch.setattr(cli_module, "ConsoleNotifier", _RecordingConsoleNotifier)

    call_count = 0

    async def _failing_run_once() -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("simulated collaborator failure")

    async def _stop_after_one_sleep(seconds: float) -> None:
        raise _StopServeLoop

    with pytest.raises(_StopServeLoop):
        await cli_module.serve_main_async(
            1.0,
            print_fn=lambda _text: None,
            sleep_fn=_stop_after_one_sleep,
            run_once_fn=_failing_run_once,
        )

    assert call_count == 1
    assert len(notifications) == 1
    assert "simulated collaborator failure" in notifications[0]


async def test_process_main_async_passes_no_discovery_source_and_never_touches_gmail(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Issue #86's actual bug report, at the wiring level: `process_main_async()` must call
    `run_once()` with `discovery_source=None` and must never call any Gmail-credential-resolving
    function -- proving `process` genuinely requires no Gmail config at all. Mocks `run_once()`
    itself (mirroring `test_cli_defaults_to_console_notifier_when_backend_unset`'s existing
    pattern in this file) rather than driving a real console approval prompt end to end: `input`'s
    default-parameter binding happens at `cli.py`'s import time, not call time, so
    `monkeypatch.setattr("builtins.input", ...)` cannot reach `prompt_recipient_email()`'s/
    `ConsoleNotifier`'s already-bound `input_fn=input` defaults -- the same reason no existing test
    in this file drives `main_async()` through a real approval prompt either."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("RTBF_TARGET_EMAIL", "user@example.com")
    monkeypatch.delenv("RTBF_GMAIL_CLIENT_SECRETS_FILE", raising=False)
    monkeypatch.delenv("RTBF_NOTIFIER_BACKEND", raising=False)
    # CI (.github/workflows/ci.yml) sets RTBF_SEARXNG_BASE_URL for an unrelated integration test's
    # job-wide env -- deleted here for determinism (whether SearXNG happens to be configured is
    # irrelevant to what this test verifies): _build_jurisdiction_detector()/
    # _build_recipient_detector() always build a detector now regardless, but run_once() itself is
    # mocked below, so neither one's collaborators (including HTTPWebFetchProvider) ever actually
    # gets called against the real network either way.
    monkeypatch.delenv("RTBF_SEARXNG_BASE_URL", raising=False)
    monkeypatch.delenv("RTBF_SEARXNG_API_KEY", raising=False)

    def _must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("process_main_async() must never touch Gmail credentials")

    monkeypatch.setattr(cli_module, "_build_gmail_discovery_source", _must_not_run)
    monkeypatch.setattr(cli_module, "_resolve_gmail_credentials", _must_not_run)
    monkeypatch.setattr(cli_module, "_build_llm_provider", lambda: MockLLMProvider())
    monkeypatch.setattr(
        cli_module,
        "resolve_llm_config",
        lambda: LLMConfig(
            provider="anthropic",
            protocol="anthropic",
            base_url="https://api.anthropic.com",
            api_key="test-key",
            model="test-model",
        ),
    )
    monkeypatch.setattr(cli_module, "SMTPSender", lambda: _SpySender())

    captured: dict[str, object] = {}

    async def _fake_run_once(
        session_, discovery_source_, target_email_, *_args: object, **_kwargs: object
    ) -> None:
        captured["discovery_source"] = discovery_source_
        captured["target_email"] = target_email_

    monkeypatch.setattr(cli_module, "run_once", _fake_run_once)

    await cli_module.process_main_async()

    assert captured["discovery_source"] is None
    assert captured["target_email"] == "user@example.com"


def test_shell_process_command_dispatches_to_process_main_async(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interactive shell's `process` command (issue #86) runs `process_main_async()` -- the
    same no-Gmail pipeline `test_process_main_async_passes_no_discovery_source_and_never_touches_
    gmail` verifies end to end at the wiring level."""
    ran: dict[str, bool] = {}

    async def _fake_process_main_async(*, verbose: bool = False) -> None:
        ran["process"] = True

    monkeypatch.setattr(cli_module, "process_main_async", _fake_process_main_async)

    _run_shell(monkeypatch, _make_shell_engine(), ["process", "exit"])

    assert ran == {"process": True}


def test_shell_process_command_reports_a_real_failure_and_keeps_the_shell_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real report: a misconfigured LLM model (or any other real-world failure -- a network error,
    an SMTP auth failure, ...) during `process` must be reported as a clean error line, not crash
    the whole interactive shell with a raw traceback -- the same class of fix already applied to
    `add`'s missing-`RTBF_TARGET_EMAIL` case, broadened here beyond just `LookupError` since
    `process` runs real LLM/SMTP calls that can fail in far more ways than a missing env var."""

    async def _fake_process_main_async(*, verbose: bool = False) -> None:
        raise RuntimeError("API returned 404: model 'mock-classify' does not exist")

    monkeypatch.setattr(cli_module, "process_main_async", _fake_process_main_async)

    printed = _run_shell(monkeypatch, _make_shell_engine(), ["process", "help", "exit"])

    assert any("error: API returned 404" in line for line in printed)
    assert any("hint:" in line and "known models" in line for line in printed)
    assert any("process" in line for line in printed)  # help still ran after the error


def test_shell_process_command_stops_on_keyboard_interrupt_and_returns_to_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single `process` pass can run for a while (classify/draft/send/follow-up over a real
    LLM/SMTP/Gmail connection, potentially across many cases) -- Ctrl-C during it must stop just
    this command and return to the prompt, the same as `scan-inbox`'s/`serve`'s existing
    `KeyboardInterrupt` handling, not propagate up and crash the whole shell (`KeyboardInterrupt`
    is a `BaseException`, so the broad `except Exception` this command also has would never have
    caught it on its own)."""

    async def _raising_process_main_async(*, verbose: bool = False) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "process_main_async", _raising_process_main_async)

    printed = _run_shell(monkeypatch, _make_shell_engine(), ["process", "help"])

    assert any("interrupted" in line for line in printed)
    assert any("Available commands" in line for line in printed)


def test_shell_process_command_omits_model_hint_for_unrelated_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unknown-model hint is specific to 404-model errors -- an unrelated failure (e.g. a
    network timeout) must not print a misleading hint about the model not being recognized."""

    async def _fake_process_main_async(*, verbose: bool = False) -> None:
        raise RuntimeError("connection timed out")

    monkeypatch.setattr(cli_module, "process_main_async", _fake_process_main_async)

    printed = _run_shell(monkeypatch, _make_shell_engine(), ["process", "exit"])

    assert any("error: connection timed out" in line for line in printed)
    assert not any("hint:" in line for line in printed)


def test_shell_scan_inbox_command_dispatches_to_main_async(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interactive shell's `scan-inbox` command (the "shell is the only CLI surface" rework's
    one genuinely new shell command -- every other former standalone subcommand already had a
    same-named or `add-case`->`add` shell equivalent) runs `main_async()` -- the same whole-inbox
    discover/draft/send pass `scan-inbox` always ran, unchanged, just reachable from inside the
    shell now instead of only as a standalone invocation."""
    ran: dict[str, bool] = {}

    async def _fake_main_async() -> None:
        ran["scan_inbox"] = True

    monkeypatch.setattr(cli_module, "main_async", _fake_main_async)

    _run_shell(monkeypatch, _make_shell_engine(), ["scan-inbox", "exit"])

    assert ran == {"scan_inbox": True}


def test_shell_scan_inbox_command_reports_a_real_failure_and_keeps_the_shell_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors `process`'s identical fix: a real failure (Gmail OAuth, LLM, SMTP transport error)
    during `scan-inbox` must be reported as a clean error line, not crash the whole interactive
    shell with a raw traceback."""

    async def _fake_main_async() -> None:
        raise RuntimeError("API returned 404: model 'mock-classify' does not exist")

    monkeypatch.setattr(cli_module, "main_async", _fake_main_async)

    printed = _run_shell(monkeypatch, _make_shell_engine(), ["scan-inbox", "help", "exit"])

    assert any("error: API returned 404" in line for line in printed)
    assert any("hint:" in line and "known models" in line for line in printed)
    assert any("scan-inbox" in line for line in printed)  # help still ran after the error


def test_shell_scan_inbox_command_omits_model_hint_for_unrelated_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors `process`'s identical test: the unknown-model hint must not appear for an unrelated
    failure (e.g. a network timeout)."""

    async def _fake_main_async() -> None:
        raise RuntimeError("connection timed out")

    monkeypatch.setattr(cli_module, "main_async", _fake_main_async)

    printed = _run_shell(monkeypatch, _make_shell_engine(), ["scan-inbox", "exit"])

    assert any("error: connection timed out" in line for line in printed)
    assert not any("hint:" in line for line in printed)


def test_shell_scan_inbox_command_stops_on_keyboard_interrupt_and_returns_to_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OCR review finding: `main_async()` can run for a while (a real Gmail scan plus a full
    classify/draft/send/follow-up pass) -- Ctrl-C during it must stop just this command and return
    to the prompt, the same as `serve`'s existing `KeyboardInterrupt` handling, not propagate up
    and crash the whole shell (`KeyboardInterrupt` is a `BaseException`, so the broad
    `except Exception` this command also has would never have caught it on its own)."""

    async def _raising_main_async() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "main_async", _raising_main_async)

    printed = _run_shell(monkeypatch, _make_shell_engine(), ["scan-inbox", "help"])

    assert any("interrupted" in line for line in printed)
    assert any("Available commands" in line for line in printed)


def test_shell_add_command_reports_a_real_failure_and_keeps_the_shell_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `add` sibling of the `process` fix above: any unexpected failure inside
    `add_case_for_url()` (not just a missing `RTBF_TARGET_EMAIL`) must also leave the shell
    running rather than crashing it."""
    monkeypatch.setenv("RTBF_TARGET_EMAIL", "user@example.com")
    _mock_add_case_collaborators(monkeypatch)

    async def _fake_add_case_for_url(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cli_module, "add_case_for_url", _fake_add_case_for_url)

    printed = _run_shell(
        monkeypatch, _make_shell_engine(), ["add https://example.com", "help", "exit"]
    )

    assert any("error: database is locked" in line for line in printed)
    assert any("add <url>" in line for line in printed)  # help still ran after the error


def test_shell_process_command_without_target_email_configured_reports_error_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RTBF_TARGET_EMAIL", raising=False)
    printed = _run_shell(monkeypatch, _make_shell_engine(), ["process", "help", "exit"])
    assert any("error: no target email configured" in line for line in printed)
    assert any("process" in line for line in printed)  # help still ran after the error


async def test_scan_inbox_behavior_unchanged_by_process_existing(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """Regression: `run_once()` with a real discovery source still discovers, creates, and
    processes cases exactly as before issue #86 -- the optional-`discovery_source` change is
    additive, `scan-inbox`'s own behavior is untouched."""
    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    llm = MockLLMProvider()
    io = _FakeIO(answers=["", ""])
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
    )
    case = session.exec(select(Case)).one()
    case_actions.confirm_recipient(session, case.id, "privacy@example-corp.com")
    case_actions.approve_draft(session, case.id)
    session.commit()

    await run_once(
        session,
        None,
        "user@example.com",
        llm,
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
    )

    cases = session.exec(select(Case)).all()
    assert len(cases) == 1
    assert cases[0].state == CaseState.AWAITING_RESPONSE
    assert len(sender.sent) == 1


def test_shell_setup_command_dispatches_to_run_setup_wizard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: dict[str, bool] = {}

    async def _fake_run_setup_wizard(*, input_fn, print_fn) -> None:
        ran["setup"] = True

    monkeypatch.setattr(cli_module, "run_setup_wizard", _fake_run_setup_wizard)

    _run_shell(monkeypatch, _make_shell_engine(), ["setup", "exit"])

    assert ran == {"setup": True}


def test_shell_setup_command_reports_a_real_failure_and_keeps_the_shell_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_run_setup_wizard(*, input_fn, print_fn) -> None:
        raise RuntimeError("docker not found")

    monkeypatch.setattr(cli_module, "run_setup_wizard", _fake_run_setup_wizard)

    printed = _run_shell(monkeypatch, _make_shell_engine(), ["setup", "help", "exit"])

    assert any("error: docker not found" in line for line in printed)
    assert any("setup" in line for line in printed)  # help still ran after the error


def test_shell_stop_command_dispatches_to_stop_services(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: dict[str, bool] = {}

    async def _fake_stop_services(*, print_fn) -> None:
        ran["stop"] = True

    monkeypatch.setattr(cli_module, "stop_services", _fake_stop_services)

    _run_shell(monkeypatch, _make_shell_engine(), ["stop", "exit"])

    assert ran == {"stop": True}


def test_shell_stop_command_reports_a_real_failure_and_keeps_the_shell_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_stop_services(*, print_fn) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli_module, "stop_services", _fake_stop_services)

    printed = _run_shell(monkeypatch, _make_shell_engine(), ["stop", "help", "exit"])

    assert any("error: boom" in line for line in printed)
    assert any("stop" in line for line in printed)  # help still ran after the error


# --- --verbose (issue #98): opt-in terminal visibility into a process/serve pass --------------


async def test_run_once_default_is_silent_beyond_pre_existing_output(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """The core additive/opt-in guarantee: with no `verbose` argument (the default), a full
    discover -> draft -> approve -> send pass prints nothing new -- exactly the pre-existing
    `prompt_case_overrides()` lines this file's other tests already assert on, and nothing else."""
    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    io = _FakeIO(answers=["", ""])
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        MockLLMProvider(),
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
    )
    case = session.exec(select(Case)).one()
    case_actions.confirm_recipient(session, case.id, "privacy@example-corp.com")
    case_actions.approve_draft(session, case.id)
    session.commit()

    await run_once(
        session,
        None,
        "user@example.com",
        MockLLMProvider(),
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
    )

    session.refresh(case)
    assert case.state == CaseState.AWAITING_RESPONSE
    assert len(sender.sent) == 1
    # None of the new verbose-only phrasing appears anywhere in the default-mode transcript.
    joined = "\n".join(io.printed)
    for phrase in ("advanced:", "request sent:", "reply found and classified"):
        assert phrase not in joined


async def test_run_once_verbose_prints_a_case_advancing_through_run_cycle(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    io = _FakeIO(answers=["", ""])
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        MockLLMProvider(),
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
        verbose=True,
    )

    case = session.exec(select(Case)).one()
    assert case.state == CaseState.DRAFT_READY
    assert any(
        "example.com" in line and "advanced:" in line and "'Discovered'" in line
        for line in io.printed
    )
    assert any("'Draft ready'" in line for line in io.printed)


async def test_run_once_verbose_prints_jurisdiction_and_recipient_proposals_recorded(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    jurisdiction_proposal = JurisdictionProposal(
        jurisdiction="GDPR",
        reasoning="The privacy policy names an EU-based data controller.",
        sources=("https://example.com/privacy",),
    )
    jurisdiction_detector = _FakeJurisdictionDetector(proposal=jurisdiction_proposal)
    recipient_proposal = RecipientProposal(
        recipient="privacy@example-corp.com",
        reasoning="The privacy policy lists this as the deletion-request contact.",
        sources=("https://example.com/privacy",),
    )
    recipient_detector = _FakeRecipientDetector(proposal=recipient_proposal)
    io = _FakeIO(answers=["", ""])  # accept the jurisdiction suggestion; blank language
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        MockLLMProvider(),
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        jurisdiction_detector=jurisdiction_detector,
        recipient_detector=recipient_detector,
        model="test-model",
        verbose=True,
    )

    assert any("jurisdiction proposal recorded" in line and "'GDPR'" in line for line in io.printed)
    assert any(
        "recipient proposal recorded" in line and "privacy@example-corp.com" in line
        for line in io.printed
    )


async def test_run_once_verbose_prints_a_case_actually_sent(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    case = Case(
        site="Example Corp",
        domain="example.com",
        target_email="user@example.com",
        recipient_email="privacy@example-corp.com",
        applicable_jurisdiction="GDPR",
        drafting_language="en",
        state=CaseState.DRAFT_READY,
    )
    session.add(case)
    session.commit()
    session.refresh(case)
    assert case.id is not None
    session.add(
        create_event(
            case_id=case.id,
            kind="draft_proposed",
            actor="agent",
            detail={
                "draft": "Please delete my personal data pursuant to GDPR Art. 17.",
                "subject": "Deletion request for example.com",
                "jurisdiction": "GDPR",
                "drafting_language": "en",
            },
        )
    )
    session.commit()
    case_actions.approve_draft(session, case.id)

    io = _FakeIO(answers=[])
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        None,
        "user@example.com",
        MockLLMProvider(),
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
        verbose=True,
    )

    session.refresh(case)
    assert case.state == CaseState.AWAITING_RESPONSE
    assert len(sender.sent) == 1
    assert any(
        "example.com" in line and "request sent:" in line and "'Awaiting response'" in line
        for line in io.printed
    )


async def test_run_once_verbose_reports_follow_up_pass_skipped_with_a_reason(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """When `reply_monitor`/`response_classifier` are omitted (e.g. no cached Gmail token yet --
    issue #99's concern, untouched here), verbose mode must say the follow-up pass didn't run this
    invocation, rather than silently doing nothing -- the real report this issue exists to fix."""
    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    io = _FakeIO(answers=["", ""])
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        MockLLMProvider(),
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
        verbose=True,
    )

    assert any(
        "follow-up pass skipped" in line
        and "reply_monitor" in line
        and "response_classifier" in line
        for line in io.printed
    )


async def test_run_once_verbose_prints_a_reply_found_and_classified(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    from datetime import UTC, datetime, timedelta

    discovery_source = _FakeDiscoverySource([_account("Example", "example.com")])
    io = _FakeIO(answers=["", ""])
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    reply = MailboxMessage(
        message_id="reply-1",
        from_address="Support <support@example-corp.com>",
        subject="Re: your deletion request",
        body="We have deleted your account and all associated data.",
        received_at=datetime.now(UTC) + timedelta(hours=1),
    )
    reply_monitor = InboxReplyMonitor(_FakeMailboxReader([reply]))
    response_classifier = ResponseClassifier(
        _CannedLLMProvider(json.dumps({"intent": "confirmation", "reasoning": "Deleted."})),
        model="mock",
    )

    await run_once(
        session,
        discovery_source,
        "user@example.com",
        MockLLMProvider(),
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
    )
    case = session.exec(select(Case)).one()
    case_actions.confirm_recipient(session, case.id, "privacy@example-corp.com")
    case_actions.approve_draft(session, case.id)
    session.commit()

    await run_once(
        session,
        None,
        "user@example.com",
        MockLLMProvider(),
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        reply_monitor=reply_monitor,
        response_classifier=response_classifier,
        model="test-model",
        verbose=True,
    )

    assert any(
        "reply found and classified" in line and "intent='confirmation'" in line
        for line in io.printed
    )
    # And the "skipped" line from the other test must never appear once the collaborators *are*
    # configured -- the two outcomes are mutually exclusive within a single pass.
    assert not any("follow-up pass skipped" in line for line in io.printed)


async def test_run_once_verbose_prints_a_case_sent_from_a_pending_additional_info_draft(
    session: Session,
    legal_provider: RegistryLegalTemplateProvider,
    global_config: GlobalConfig,
) -> None:
    """The drafting-is-a-preview redesign (superseding issue #117/#119's fused, poll-triggered
    draft+send, which used to print its own "resend triggered:" line from `run_once()`'s now-
    retired additional-info pass): `run_once()` no longer has a separate pass for this at all --
    once a human explicitly drafts (`followup.draft_additional_info_response()`) and approves
    (`case_actions.approve_draft()`, which flips the case to `Draft ready`), the existing "request
    sent:" verbose line (`test_run_once_verbose_prints_a_case_actually_sent`'s exact phrasing)
    fires for it same as any other case, since it's now indistinguishable from a first-time send by
    the time `run_once()`'s generic send loop reaches it."""
    case = Case(
        site="Example Corp",
        domain="example.com",
        target_email="user@example.com",
        recipient_email="privacy@example-corp.com",
        applicable_jurisdiction="GDPR",
        drafting_language="en",
        state=CaseState.REQUIRES_HUMAN_ACTION,
    )
    session.add(case)
    session.commit()
    session.refresh(case)
    assert case.id is not None
    session.add(
        create_event(
            case_id=case.id,
            kind="draft_proposed",
            actor="agent",
            detail={
                "draft": "Please delete my personal data pursuant to GDPR Art. 17.",
                "subject": "Deletion request for example.com",
                "jurisdiction": "GDPR",
                "drafting_language": "en",
            },
        )
    )
    session.add(create_event(case_id=case.id, kind="request_sent", actor="agent"))
    session.add(
        create_event(
            case_id=case.id,
            kind="additional_info_requested",
            actor="agent",
            detail={"reasoning": "site asked for date of birth"},
        )
    )
    session.commit()
    case_actions.provide_text_response(session, case.id, "my date of birth is 1990-01-01")
    await draft_additional_info_response(session, case, MockLLMProvider(), model="test-model")
    session.commit()
    case_actions.approve_draft(session, case.id)
    session.refresh(case)
    assert case.state == CaseState.DRAFT_READY

    io = _FakeIO(answers=[])
    notifier = ConsoleNotifier(print_fn=io.print_fn)
    sender = _SpySender()

    await run_once(
        session,
        None,
        "user@example.com",
        MockLLMProvider(),
        notifier,
        legal_provider,
        global_config,
        sender,
        print_fn=io.print_fn,
        model="test-model",
        verbose=True,
    )

    session.refresh(case)
    assert case.state == CaseState.AWAITING_RESPONSE
    assert any(
        "example.com" in line and "request sent:" in line and "'Awaiting response'" in line
        for line in io.printed
    )


# --- _extract_verbose_flag() shell-token helper (issue #98) ------------------------------------


def test_extract_verbose_flag_finds_long_form_anywhere_in_rest() -> None:
    assert cli_module._extract_verbose_flag("--verbose") == (True, "")
    assert cli_module._extract_verbose_flag("5 --verbose") == (True, "5")
    assert cli_module._extract_verbose_flag("--verbose 5") == (True, "5")


def test_extract_verbose_flag_finds_short_form() -> None:
    assert cli_module._extract_verbose_flag("-v") == (True, "")
    assert cli_module._extract_verbose_flag("5 -v") == (True, "5")


def test_extract_verbose_flag_absent_leaves_rest_untouched() -> None:
    assert cli_module._extract_verbose_flag("5") == (False, "5")
    assert cli_module._extract_verbose_flag("") == (False, "")


# --- shell process/serve --verbose wiring (issue #98) ------------------------------------------


def test_shell_process_verbose_flag_passes_verbose_true(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_process_main_async(*, verbose: bool = False) -> None:
        captured["verbose"] = verbose

    monkeypatch.setattr(cli_module, "process_main_async", _fake_process_main_async)

    _run_shell(monkeypatch, _make_shell_engine(), ["process --verbose", "exit"])

    assert captured["verbose"] is True


def test_shell_process_short_verbose_flag_passes_verbose_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _fake_process_main_async(*, verbose: bool = False) -> None:
        captured["verbose"] = verbose

    monkeypatch.setattr(cli_module, "process_main_async", _fake_process_main_async)

    _run_shell(monkeypatch, _make_shell_engine(), ["process -v", "exit"])

    assert captured["verbose"] is True


def test_shell_process_without_flag_passes_verbose_false(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_process_main_async(*, verbose: bool = False) -> None:
        captured["verbose"] = verbose

    monkeypatch.setattr(cli_module, "process_main_async", _fake_process_main_async)

    _run_shell(monkeypatch, _make_shell_engine(), ["process", "exit"])

    assert captured["verbose"] is False


def test_shell_serve_verbose_flag_alongside_an_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_serve_main_async(interval_minutes, **kwargs) -> None:
        captured["interval_minutes"] = interval_minutes
        captured["verbose"] = kwargs.get("verbose")

    monkeypatch.setattr(cli_module, "serve_main_async", _fake_serve_main_async)

    _run_shell(monkeypatch, _make_shell_engine(), ["serve 5 --verbose", "exit"])

    assert captured["interval_minutes"] == 5.0
    assert captured["verbose"] is True


def test_shell_serve_verbose_flag_before_the_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_serve_main_async(interval_minutes, **kwargs) -> None:
        captured["interval_minutes"] = interval_minutes
        captured["verbose"] = kwargs.get("verbose")

    monkeypatch.setattr(cli_module, "serve_main_async", _fake_serve_main_async)

    _run_shell(monkeypatch, _make_shell_engine(), ["serve --verbose 5", "exit"])

    assert captured["interval_minutes"] == 5.0
    assert captured["verbose"] is True


def test_shell_serve_without_flag_passes_verbose_false(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_serve_main_async(interval_minutes, **kwargs) -> None:
        captured["interval_minutes"] = interval_minutes
        captured["verbose"] = kwargs.get("verbose")

    monkeypatch.setattr(cli_module, "serve_main_async", _fake_serve_main_async)

    _run_shell(monkeypatch, _make_shell_engine(), ["serve", "exit"])

    assert captured["interval_minutes"] is None
    assert captured["verbose"] is False
