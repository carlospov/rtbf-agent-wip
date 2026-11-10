"""Tests for the "additional info" draft/review/send flow (issue #69, superseding #117/#119's
fused-approval/auto-poll revision -- see `rtbf_agent.followup`'s module docstring, "Why drafting is
now an explicit, synchronous action"): `rtbf_agent.agent_loop.compose_additional_info_draft`/
`_execute_send`, `rtbf_agent.followup.draft_additional_info_response`, and `rtbf_agent.case_actions.
provide_text_response`/`attach_document`.

Automated-case-loop rework, now with an explicit "Draft" step instead of a background poll: a human
records their answer via `case_actions.provide_text_response()`/`attach_document()` (kanban or CLI),
then explicitly composes a preview via `followup.draft_additional_info_response()` (kanban's "Draft"
button or the CLI's `draft-additional-info` subcommand) -- `case.state` stays `Requires human
action` throughout, and nothing is sent. Only `case_actions.approve_draft()` (widened to accept this
origin) ever flips the case to `Draft ready`, handing off to the existing, unmodified recurring
send loop. Exercised against a fake `Sender` double -- no real SMTP/network anywhere. R1 (every send
is its own fresh approval-gated act), R9 (no timeout on `Requires human action`, and no code path
here ever sends on a human's behalf), and reuse of the sensitive-document lifecycle (issue #17, not
a second document-handling path) are the restrictions under test.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from rtbf_agent import case_actions
from rtbf_agent.agent_loop import compose_additional_info_draft, send_request
from rtbf_agent.core.interfaces import EmailMessage, Sender
from rtbf_agent.core.models import Case, CaseState, Document, DocumentStatus, Event, create_event
from rtbf_agent.followup import draft_additional_info_response, selected_documents
from rtbf_agent.plugins.llm.mock import MockLLMProvider

_TEST_MODEL = "test-model"
"""Stand-in for a real, resolved LLM model name -- `MockLLMProvider` ignores `request.model`
entirely, so any string works here; matches `test_agent_loop_mock.py`'s identical constant."""


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


class _FakeSender(Sender):
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


class _RaisingSender(Sender):
    async def send(self, message: EmailMessage) -> None:
        raise OSError("connection refused")


def _make_case_requiring_human_action(session: Session) -> Case:
    """A `Case` in `Requires human action` with an original draft already on record -- as if
    `agent_loop.send_request()` sent it once, then `agent_loop.flag_requires_human_action()` moved
    it here after a site's reply asked for more information."""
    case = Case(
        site="Example Corp",
        domain="example.com",
        target_email="user@gmail.com",
        recipient_email="privacy@example-corp.com",
        applicable_jurisdiction="GDPR",
        drafting_language="en",
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
    case.state = CaseState.REQUIRES_HUMAN_ACTION
    session.add(case)
    session.commit()
    session.refresh(case)
    return case


def _events(session: Session, case_id: int, kind: str) -> list[Event]:
    """Every `Event` of `kind` for `case_id`, in insertion order.

    Sorted in Python by `id`, not `.order_by(Event.id)` -- `Event.id` is `int | None` (nullable
    until flushed), which `ty` rejects as an `order_by()` argument via the class-level column
    descriptor (same reason `agent_loop.latest_draft()` sorts in Python). Several tests here rely
    on list order/indices (e.g. `draft_events[-1]`) to mean "most recently created", so this must
    not be left to the database's unspecified-without-`ORDER BY` result ordering.
    """
    events = session.exec(select(Event).where(Event.case_id == case_id, Event.kind == kind)).all()
    return sorted(events, key=lambda event: event.id or 0)


async def _draft_then_approve_and_send(session: Session, case: Case, sender: Sender) -> Case:
    """Test helper mirroring the real flow end to end: compose a preview draft from whatever's
    currently on record, approve it (the one moment `case.state` moves, per `case_actions.
    approve_draft()`'s docstring), then let the existing send loop dispatch it -- exactly what
    `cli.run_once()`'s "send every approved Draft ready case" pass does on its next tick, called
    directly here rather than driving the whole recurring loop."""
    assert case.id is not None
    await draft_additional_info_response(session, case, MockLLMProvider(), model=_TEST_MODEL)
    session.commit()
    case_actions.approve_draft(session, case.id)
    session.refresh(case)
    documents = selected_documents(session, case.id)
    result = await send_request(session, case, sender, documents)
    session.commit()
    return result


# --------------------------------------------------------------------------------------------- #
# agent_loop.compose_additional_info_draft(): a preview only -- case.state never moves here.
# --------------------------------------------------------------------------------------------- #


async def test_compose_additional_info_draft_does_not_change_state(session):
    """The core of the drafting-is-a-preview redesign: composing from the human's notes records a
    `draft_proposed` Event but leaves `case.state` at `Requires human action` -- no approval, no
    send, nothing implicitly authorized."""
    case = _make_case_requiring_human_action(session)

    result = await compose_additional_info_draft(
        session,
        case,
        MockLLMProvider(),
        _TEST_MODEL,
        extra_text="my date of birth is 1990-01-01",
    )
    session.commit()

    assert result.state == CaseState.REQUIRES_HUMAN_ACTION
    resend_draft = _events(session, case.id, "draft_proposed")[-1].detail["draft"]
    assert "1990-01-01" in resend_draft
    # Issue #112: the resend body is just the new content -- the outgoing email is already a
    # threaded reply, so the original template text is not repeated into the body.
    assert "GDPR Art. 17" not in resend_draft
    # Nothing was authorized or sent by drafting alone.
    assert _events(session, case.id, "approval_granted") == []
    assert len(_events(session, case.id, "request_sent")) == 1  # only the fixture's original


async def test_compose_additional_info_draft_documents_only_uses_deterministic_fallback_body(
    session,
):
    case = _make_case_requiring_human_action(session)
    case_actions.attach_document(session, case.id, b"identity document bytes", "image/png")
    documents = selected_documents(session, case.id)

    result = await compose_additional_info_draft(
        session, case, MockLLMProvider(), _TEST_MODEL, documents=documents
    )
    session.commit()

    assert result.state == CaseState.REQUIRES_HUMAN_ACTION
    draft = _events(session, case.id, "draft_proposed")[-1].detail["draft"]
    assert "document" in draft.lower()


async def test_compose_additional_info_draft_rejects_outside_requires_human_action(session):
    case = _make_case_requiring_human_action(session)
    case.state = CaseState.AWAITING_RESPONSE
    session.add(case)
    session.commit()

    with pytest.raises(ValueError, match="Requires human action"):
        await compose_additional_info_draft(
            session, case, MockLLMProvider(), _TEST_MODEL, extra_text="here you go"
        )


async def test_compose_additional_info_draft_rejects_neither_extra_text_nor_document(session):
    case = _make_case_requiring_human_action(session)

    with pytest.raises(ValueError, match="at least one of"):
        await compose_additional_info_draft(session, case, MockLLMProvider(), _TEST_MODEL)


async def test_two_successive_additional_info_drafts_do_not_compound_prior_round_bodies(session):
    """Issue #112 regression, carried over to the new explicit-drafting flow: round two's body must
    not repeat round one's `extra_text`, or the original legal-template text, since each drafted
    body is just that round's new content and the outgoing email is already threaded (In-Reply-To/
    References) so the recipient's mail client shows prior rounds without the body restating
    them."""
    case = _make_case_requiring_human_action(session)
    assert case.id is not None

    case_actions.provide_text_response(session, case.id, "my date of birth is 1990-01-01")
    round_one = await _draft_then_approve_and_send(session, case, _FakeSender())
    assert round_one.state == CaseState.AWAITING_RESPONSE

    # Site asks again -- back to Requires human action for a second round, this time with a
    # document instead of text (case_actions/document_store's normal attach-then-select path).
    session.add(
        create_event(case_id=case.id, kind="additional_info_requested", actor="agent", detail={})
    )
    case.state = CaseState.REQUIRES_HUMAN_ACTION
    session.add(case)
    session.commit()
    session.refresh(case)

    case_actions.attach_document(session, case.id, b"identity document bytes", "image/png")
    documents = selected_documents(session, case.id)
    assert len(documents) == 1

    round_two = await _draft_then_approve_and_send(session, case, _FakeSender())

    assert round_two.state == CaseState.AWAITING_RESPONSE
    draft_events = _events(session, case.id, "draft_proposed")
    assert len(draft_events) == 3  # original + round one + round two
    round_two_draft = draft_events[-1].detail["draft"]
    assert "1990-01-01" not in round_two_draft
    assert "GDPR Art. 17" not in round_two_draft
    assert "document" in round_two_draft


# --------------------------------------------------------------------------------------------- #
# followup.draft_additional_info_response(): resolves the human's current fresh answer and calls
# compose_additional_info_draft() -- the explicit-action replacement for the retired background
# poll, check_for_pending_additional_info().
# --------------------------------------------------------------------------------------------- #


async def test_draft_additional_info_response_uses_a_fresh_text_answer(session):
    case = _make_case_requiring_human_action(session)
    case_actions.provide_text_response(session, case.id, "my date of birth is 1990-01-01")

    result = await draft_additional_info_response(
        session, case, MockLLMProvider(), model=_TEST_MODEL
    )
    session.commit()

    assert result.state == CaseState.REQUIRES_HUMAN_ACTION
    draft = _events(session, case.id, "draft_proposed")[-1].detail["draft"]
    assert "1990-01-01" in draft


async def test_draft_additional_info_response_uses_a_fresh_document_alone(session):
    case = _make_case_requiring_human_action(session)
    case_actions.attach_document(session, case.id, b"identity document bytes", "image/png")

    result = await draft_additional_info_response(
        session, case, MockLLMProvider(), model=_TEST_MODEL
    )
    session.commit()

    assert result.state == CaseState.REQUIRES_HUMAN_ACTION
    draft = _events(session, case.id, "draft_proposed")[-1].detail["draft"]
    assert "document" in draft.lower()


async def test_draft_additional_info_response_uses_both_a_document_and_pending_text_together(
    session,
):
    """The XOR/tie-break is gone (LLM-drafted-replies extension): if a human used both channels in
    the same round, both are resolved and passed through together -- the text drives the LLM-
    drafted body, the document is still attached once the draft is approved and sent."""
    case = _make_case_requiring_human_action(session)
    case_actions.provide_text_response(session, case.id, "text answer")
    case_actions.attach_document(session, case.id, b"doc bytes", "image/png")
    sender = _FakeSender()

    result = await _draft_then_approve_and_send(session, case, sender)

    assert result.state == CaseState.AWAITING_RESPONSE
    draft = _events(session, case.id, "draft_proposed")[-1].detail["draft"]
    # extra_text is present, so the body is LLM-drafted (MockLLMProvider echoes the prompt, which
    # carries the human's text verbatim), not the bare "please find attached" template text.
    assert "text answer" in draft
    assert len(sender.sent) == 1
    assert len(sender.sent[0].attachments) == 1  # the document was still attached alongside it


async def test_a_draft_can_attach_more_than_one_document_at_once(session):
    """The actual point of the vault (issue #111): a human can select several documents for one
    round, not just the single most-recent upload -- and each is still attached once the draft is
    approved and sent, exactly as before drafting became a separate, explicit step."""
    from rtbf_agent import document_store

    case = _make_case_requiring_human_action(session)
    case_actions.attach_document(session, case.id, b"id card bytes", "image/png", label="ID card")
    id_card = session.exec(select(Document)).one()
    proof_doc = document_store.store_document(
        session, case, b"proof of address bytes", "application/pdf", label="Proof of address"
    )
    session.commit()
    case_actions.select_documents_for_resend(session, case.id, [id_card.id, proof_doc.id])

    sender = _FakeSender()
    result = await _draft_then_approve_and_send(session, case, sender)

    assert result.state == CaseState.AWAITING_RESPONSE
    assert len(sender.sent) == 1
    attachments = sender.sent[0].attachments
    assert len(attachments) == 2
    assert {attachment.content for attachment in attachments} == {
        b"id card bytes",
        b"proof of address bytes",
    }
    assert {attachment.filename for attachment in attachments} == {"ID card", "Proof of address"}


async def test_draft_additional_info_response_ignores_a_document_left_over_from_an_earlier_round(
    session,
):
    """Real report: a document uploaded for an earlier `additional_info_requested` round that
    somehow never got consumed must not get silently attached to a *later*, unrelated round asking
    for something else -- that would answer the wrong question with stale, previously-uploaded
    sensitive content. A document only counts as pending if it was uploaded after the *current*
    round's request, so with nothing fresh for round 2, drafting refuses rather than guessing."""
    case = _make_case_requiring_human_action(session)
    # Round 1's leftover, never-consumed document.
    case_actions.attach_document(session, case.id, b"round 1 identity document", "image/png")

    # Round 2 begins: a later additional_info_requested event, asking for something else --
    # nothing new uploaded in response to it yet.
    session.add(
        create_event(
            case_id=case.id,
            kind="additional_info_requested",
            actor="agent",
            detail={"reasoning": "site asked for something else entirely"},
        )
    )
    session.commit()

    with pytest.raises(ValueError, match="no fresh document selection or text response"):
        await draft_additional_info_response(session, case, MockLLMProvider(), model=_TEST_MODEL)

    # Left completely untouched (R9) -- only the fixture's own original draft_proposed event.
    assert len(_events(session, case.id, "draft_proposed")) == 1


async def test_draft_additional_info_response_uses_a_document_uploaded_for_the_current_round(
    session,
):
    """The counterpart of the stale-document test above: a document uploaded (and selected) *after*
    the current round's request is genuinely the answer to it, and must still be picked up -- even
    with an older, already-stale selection from an earlier round also on record, and its document
    still `AVAILABLE`."""
    case = _make_case_requiring_human_action(session)
    case_actions.attach_document(session, case.id, b"round 1 identity document", "image/png")

    session.add(
        create_event(
            case_id=case.id,
            kind="additional_info_requested",
            actor="agent",
            detail={"reasoning": "site asked for something else entirely"},
        )
    )
    session.commit()

    case_actions.attach_document(session, case.id, b"round 2 identity document", "image/png")
    current_documents = selected_documents(session, case.id)
    assert len(current_documents) == 1
    assert (
        current_documents[0].sha256_hash == hashlib.sha256(b"round 2 identity document").hexdigest()
    )

    sender = _FakeSender()
    result = await _draft_then_approve_and_send(session, case, sender)

    assert result.state == CaseState.AWAITING_RESPONSE
    draft_events = _events(session, case.id, "draft_proposed")
    assert len(draft_events) == 2  # the original, plus this round's
    assert len(sender.sent) == 1
    assert len(sender.sent[0].attachments) == 1
    assert sender.sent[0].attachments[0].content == b"round 2 identity document"

    # The stale round-1 document is untouched (still available, never silently sent).
    stale_document = session.exec(
        select(Document).where(Document.case_id == case.id, Document.id != current_documents[0].id)
    ).one()
    assert stale_document.status == DocumentStatus.AVAILABLE


async def test_draft_additional_info_response_rejects_with_nothing_recorded_yet(session):
    case = _make_case_requiring_human_action(session)

    with pytest.raises(ValueError, match="no fresh document selection or text response"):
        await draft_additional_info_response(session, case, MockLLMProvider(), model=_TEST_MODEL)


async def test_draft_additional_info_response_rejects_outside_requires_human_action(session):
    case = _make_case_requiring_human_action(session)
    case.state = CaseState.AWAITING_RESPONSE
    session.add(case)
    session.commit()

    with pytest.raises(ValueError, match="Requires human action"):
        await draft_additional_info_response(session, case, MockLLMProvider(), model=_TEST_MODEL)


# --------------------------------------------------------------------------------------------- #
# Full flow: draft -> approve/reject -> the existing, unmodified send loop.
# --------------------------------------------------------------------------------------------- #


async def test_reject_after_a_draft_moves_straight_to_rejected_escalated(session):
    """A drafted-but-not-yet-approved additional-info round is its own fresh decision -- rejecting
    it moves straight to `Rejected/Escalated`, exactly like rejecting any other draft, even though
    `case.state` never left `Requires human action` for the drafting step itself."""
    case = _make_case_requiring_human_action(session)
    case_actions.provide_text_response(session, case.id, "here you go")
    await draft_additional_info_response(session, case, MockLLMProvider(), model=_TEST_MODEL)
    session.commit()
    assert case.state == CaseState.REQUIRES_HUMAN_ACTION

    result = case_actions.reject_draft(session, case.id)

    assert result.state == CaseState.REJECTED_ESCALATED


async def test_send_failure_after_approving_an_additional_info_draft_leaves_it_at_draft_ready(
    session,
):
    """Approving a drafted additional-info round flips `case.state` to `Draft ready` (the one
    moment it moves, per `case_actions.approve_draft()`'s docstring) and hands off to the existing
    send loop -- a transient `Sender` failure there leaves the case at `Draft ready` with its
    approval already on record, exactly like any other send failure, never silently retried or lost
    (R1)."""
    case = _make_case_requiring_human_action(session)
    case_actions.provide_text_response(session, case.id, "here you go")
    await draft_additional_info_response(session, case, MockLLMProvider(), model=_TEST_MODEL)
    session.commit()
    case_actions.approve_draft(session, case.id)
    session.refresh(case)
    assert case.state == CaseState.DRAFT_READY

    with pytest.raises(OSError, match="connection refused"):
        await send_request(session, case, _RaisingSender())

    session.refresh(case)
    assert case.state == CaseState.DRAFT_READY  # never reached Awaiting response
    assert len(_events(session, case.id, "approval_granted")) == 1  # recorded despite the failure
    # Exactly one request_sent Event -- the original send's, from the fixture; the failed attempt
    # recorded no new one.
    assert len(_events(session, case.id, "request_sent")) == 1


async def test_multiple_rounds_of_additional_info_requests_are_each_independently_approval_gated(
    session,
):
    case = _make_case_requiring_human_action(session)

    case_actions.provide_text_response(session, case.id, "round one")
    await _draft_then_approve_and_send(session, case, _FakeSender())
    session.refresh(case)
    assert case.state == CaseState.AWAITING_RESPONSE

    # The site asks again -- a second additional_info_requested Event, back to Requires human
    # action, exactly as issue #68's flag_requires_human_action() would do on a second reply.
    session.add(
        create_event(case_id=case.id, kind="additional_info_requested", actor="agent", detail={})
    )
    case.state = CaseState.REQUIRES_HUMAN_ACTION
    session.add(case)
    session.commit()
    session.refresh(case)

    case_actions.provide_text_response(session, case.id, "round two")
    result = await _draft_then_approve_and_send(session, case, _FakeSender())

    assert result.state == CaseState.AWAITING_RESPONSE
    approval_events = _events(session, case.id, "approval_granted")
    assert (
        len(approval_events) == 2
    )  # two independent approvals -- the second never reused the first
    draft_events = _events(session, case.id, "draft_proposed")
    assert len(draft_events) == 3  # original + round one + round two
    assert "round one" in draft_events[1].detail["draft"]
    assert "round two" in draft_events[2].detail["draft"]


# --------------------------------------------------------------------------------------------- #
# Recording-only actions (unaffected by the drafting-is-a-preview redesign).
# --------------------------------------------------------------------------------------------- #


async def test_provide_text_response_rejects_blank_text(session):
    case = _make_case_requiring_human_action(session)

    with pytest.raises(case_actions.InvalidInputError):
        case_actions.provide_text_response(session, case.id, "   ")


async def test_attach_document_rejects_empty_content(session):
    case = _make_case_requiring_human_action(session)

    with pytest.raises(case_actions.InvalidInputError):
        case_actions.attach_document(session, case.id, b"", "image/png")


async def test_provide_text_response_is_refused_outside_requires_human_action(session):
    case = _make_case_requiring_human_action(session)
    case.state = CaseState.AWAITING_RESPONSE
    session.add(case)
    session.commit()

    with pytest.raises(case_actions.InvalidCaseStateError):
        case_actions.provide_text_response(session, case.id, "here you go")


async def test_send_request_still_works_unchanged_with_no_document_argument(session):
    """Regression guard: extending send_request()/_execute_send() with an optional `document`
    parameter (issue #69) must not change behavior for the original, document-less send path."""
    case = Case(
        site="Example",
        domain="example.com",
        target_email="user@example.com",
        recipient_email="privacy@example.com",
    )
    session.add(case)
    session.commit()
    session.refresh(case)
    session.add(
        create_event(
            case_id=case.id,
            kind="draft_proposed",
            actor="agent",
            detail={"draft": "body", "subject": "subject"},
        )
    )
    case.state = CaseState.DRAFT_READY
    session.add(case)
    session.commit()
    case_actions.approve_draft(session, case.id)

    sender = _FakeSender()
    result = await send_request(session, case, sender)

    assert result.state == CaseState.AWAITING_RESPONSE
    assert sender.sent[0].attachments == ()


async def test_execute_send_allows_a_document_originally_uploaded_on_a_different_case(session):
    """Issue #111: unlike the pre-vault behavior, `_execute_send()` no longer refuses a document
    whose `case_id` doesn't match the case being sent for -- reusing a vault document across cases
    is exactly the vault's point (see `Document`'s own docstring for why no schema change was
    needed for this)."""
    from rtbf_agent.document_store import store_document

    case = _make_case_requiring_human_action(session)
    other_case = Case(site="Other", domain="other.com", target_email="user@example.com")
    session.add(other_case)
    session.commit()
    session.refresh(other_case)

    reused_document = store_document(session, other_case, b"content", mime_type="image/png")
    session.commit()

    from rtbf_agent.agent_loop import _execute_send, _existing_approval_granted_event

    case.state = CaseState.DRAFT_READY
    session.add(case)
    session.commit()
    case_actions.approve_draft(session, case.id)
    approval_event = _existing_approval_granted_event(session, case.id)

    sender = _FakeSender()
    await _execute_send(session, case, approval_event, sender, [reused_document])

    assert len(sender.sent) == 1
    assert len(sender.sent[0].attachments) == 1
    assert sender.sent[0].attachments[0].content == b"content"
