"""Shared decision-recording layer for every case-action interface (kanban web routes, CLI
subcommands, and later Telegram buttons) -- one place per decision, so approving a draft,
confirming a recipient, confirming a follow-up outcome, or supplying additional info is never
reimplemented per channel (real report; see `tasks/todo.md`'s "Automated case loop" section for
the full design this module is part of).

Every function here is a plain, synchronous, DB-only decision recorder: it looks up and gates on
`Case` state, writes the `Event` `agent_loop`/`followup` already establish (or flips state
directly for a terminal, no-further-work decision like reject/dispute), and does nothing else --
no notifier calls, no LLM calls, no SMTP, no blocking. Turning a *recorded* decision into a real
action (sending an email, applying a follow-up transition, resending with new information) is the
recurring loop's job (`cli.run_once()`), never this module's.

Raises plain exceptions with no FastAPI/argparse dependency, so both `api/*.py` (translates to an
`HTTPException` + i18n string, exactly as it already did before this module existed) and `cli.py`'s
subcommands (prints to stderr, nonzero exit) can present the same underlying failure through their
own idiom, from the same structured attributes (`case_id`, `state`, ...) rather than parsing a
message string.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlmodel import Session, col, select, update

from rtbf_agent import document_store
from rtbf_agent.agent_loop import apply_confirmed_followup_transition, latest_draft
from rtbf_agent.core.models import (
    Case,
    CaseState,
    Document,
    DocumentStatus,
    Event,
    create_event,
)
from rtbf_agent.plugins.followup.detector import ReplyIntent


class CaseActionError(Exception):
    """Base for every error a `case_actions` function can raise."""


class CaseNotFoundError(CaseActionError):
    def __init__(self, case_id: int) -> None:
        self.case_id = case_id
        super().__init__(f"case {case_id} not found")


class InvalidCaseStateError(CaseActionError):
    """`case_id` exists but isn't in a state (or doesn't have the pending data) this action
    requires -- `expected` is a short, human-readable description of what was needed, e.g.
    `"in 'Draft ready' state"` or `"awaiting a follow-up confirmation"`."""

    def __init__(self, case_id: int, state: CaseState, expected: str) -> None:
        self.case_id = case_id
        self.state = state
        self.expected = expected
        super().__init__(f"case {case_id} is not {expected} (currently {state.value!r})")


class InvalidInputError(CaseActionError):
    """The caller-supplied input itself is invalid (e.g. a malformed email address, empty text)."""


def _get_case(session: Session, case_id: int) -> Case:
    case = session.get(Case, case_id)
    if case is None:
        raise CaseNotFoundError(case_id)
    return case


def _draft_ready_case(session: Session, case_id: int) -> Case:
    """The `Case` for `case_id`, if (and only if) it's currently in `Draft ready`.

    Moved here from `api/approval.py`'s `_draft_ready_case_or_409()` (Phase 0 of the automated-loop
    plan) -- same gate, now shared by every interface that needs it rather than just the kanban
    routes.
    """
    case = _get_case(session, case_id)
    if case.state != CaseState.DRAFT_READY:
        raise InvalidCaseStateError(case_id, case.state, "in 'Draft ready' state")
    return case


def _has_pending_additional_info_draft(session: Session, case_id: int) -> bool:
    """True iff a `draft_proposed` `Event` exists for `case_id`'s *current* `Requires human
    action` round -- i.e. a preview draft has been composed (`followup.
    draft_additional_info_response()`) since the round's `additional_info_requested` `Event`, not
    left over from an earlier, already-resolved round.

    The new origin `_send_eligible_case()` gates on, alongside the original `Draft ready` origin
    (see that function's docstring): explicit, human-triggered drafting (the "drafting is a
    preview, not a transition" redesign -- `followup.py`'s module docstring) no longer moves
    `case.state`, so a case can now have a genuinely pending, reviewable draft while still nominally
    `Requires human action`.

    Same freshness shape as `followup.selected_documents()`/`_pending_text_response()` (duplicated
    here, a plain query, rather than importing those other module's private helpers -- this
    module's own docstring is explicit that it only ever reads/writes `Event`/`Case` state itself).

    **Also requires the latest draft not already be consumed by an `approval_granted` `Event`**
    (code-reviewer finding on this PR) -- *within one continuous round*, a case reached via this
    origin is never still `Requires human action` with an already-approved draft, since
    `approve_draft()` immediately flips it to `Draft ready` the moment it's approved (see that
    function's docstring). But `case_actions.dispute_followup()`/`reopen_case()` can both return a
    case to `Requires human action` *without* recording a fresh `additional_info_requested` `Event`
    at all -- so after a prior additional-info round has already been fully approved and sent, a
    later dispute/reopen would otherwise leave that same old, already-consumed `draft_proposed`
    Event looking "fresh" against the same old `additional_info_requested` Event it was genuinely
    drafted for, and a human clicking Approve on the disputed/reopened case would silently resend
    stale, unrelated content under a brand new approval. Checked the same way
    `_has_unconsumed_approval_event()` checks staleness: any `approval_granted` Event recorded after
    the latest `draft_proposed` Event means that draft was already used for a decision, genuine or
    not, so it can never count as a fresh, unreviewed preview again.
    """
    drafted = session.exec(
        select(Event).where(Event.case_id == case_id, Event.kind == "draft_proposed")
    ).all()
    if not drafted:
        return False
    latest_draft_event = max(drafted, key=lambda event: event.id or 0)

    requested_events = session.exec(
        select(Event).where(Event.case_id == case_id, Event.kind == "additional_info_requested")
    ).all()
    if not requested_events:
        # No current round to check freshness against -- same defensive posture as
        # followup.selected_documents()/_pending_text_response().
        return False
    latest_request = max(requested_events, key=lambda event: event.id or 0)
    if (latest_draft_event.id or 0) <= (latest_request.id or 0):
        return False  # stale: drafted before the current round's request, not an answer to it

    already_consumed = any(
        (event.id or 0) > (latest_draft_event.id or 0)
        for event in session.exec(
            select(Event).where(Event.case_id == case_id, Event.kind == "approval_granted")
        ).all()
    )
    return not already_consumed


def _send_eligible_case(session: Session, case_id: int) -> Case:
    """The `Case` for `case_id`, if it's eligible for a send/reject/edit decision -- either origin
    `approve_draft()`/`reject_draft()`/`edit_draft()` now accept (the "drafting is a preview, not a
    transition" redesign -- see `followup.py`'s module docstring):

    - `Draft ready` and no unconsumed `approval_granted` `Event` yet (the original, sole origin
      before this redesign -- unchanged).
    - `Requires human action` with a pending preview draft (`_has_pending_additional_info_draft()`)
      -- new: a human can now draft, review, and edit a reply while the case is still nominally
      `Requires human action`, only ever moving to `Draft ready` once they actually approve it
      (`approve_draft()`'s docstring covers the one place that transition happens).

    `InvalidCaseStateError` otherwise. Replaces `_draft_ready_case()` at the three call sites that
    need this wider condition; `_draft_ready_case()` itself is untouched and still used by
    `confirm_recipient()` (a recipient is already settled long before a case ever reaches `Requires
    human action`, so that one has no reason to widen).
    """
    case = _get_case(session, case_id)
    if case.state == CaseState.DRAFT_READY and not _has_unconsumed_approval_event(session, case_id):
        return case
    if case.state == CaseState.REQUIRES_HUMAN_ACTION and _has_pending_additional_info_draft(
        session, case_id
    ):
        return case
    raise InvalidCaseStateError(
        case_id,
        case.state,
        "eligible to send/reject/edit ('Draft ready' and not yet approved, or 'Requires human "
        "action' with a pending draft)",
    )


def _update_recipient_if_not_approved(session: Session, case_id: int, recipient_email: str) -> bool:
    """Atomically set `Case.recipient_email` for `case_id`, but only if it's still `Draft ready`
    and no `approval_granted` `Event` exists for it yet -- returns `True` if the write happened,
    `False` if refused because the case is already approved or no longer `Draft ready`. One atomic
    `UPDATE ... WHERE ...` statement, not a separate check-then-write, to avoid a TOCTOU window
    against a concurrent approval *or* a concurrent state change (e.g. `reject_draft()` running
    between `confirm_recipient()`'s own `_draft_ready_case()` SELECT and this UPDATE) -- the
    `Case.state == Draft ready` condition was added to this WHERE clause (OCR review finding) to
    close that second race the same way the `approval_granted` subquery already closes the first
    one; moved verbatim from `api/recipient.py`, see its git history for the original finding.
    """
    result = session.exec(
        update(Case)
        .where(col(Case.id) == case_id)
        .where(col(Case.state) == CaseState.DRAFT_READY)
        .where(
            ~select(Event.id)
            .where(col(Event.case_id) == case_id, col(Event.kind) == "approval_granted")
            .exists()
        )
        .values(recipient_email=recipient_email)
    )
    return result.rowcount > 0


def _latest_proposed_recipient(session: Session, case_id: int) -> str | None:
    """The `recipient` from the most recent `recipient_proposed` `Event` for `case_id`, or `None`
    if there isn't one -- used only to compute `matched_suggestion` on the recorded confirmation
    event (an audit detail, never a gate). Moved verbatim from `api/recipient.py`."""
    events = sorted(
        session.exec(
            select(Event).where(Event.case_id == case_id, Event.kind == "recipient_proposed")
        ).all(),
        key=lambda event: event.id or 0,
    )
    if not events:
        return None
    recipient = events[-1].detail.get("recipient")
    return recipient if isinstance(recipient, str) else None


def confirm_recipient(session: Session, case_id: int, recipient_email: str) -> Case:
    """Set `Case.recipient_email` for a `Draft ready` case and record a `recipient_confirmed`
    `Event`. Can be resubmitted while the case remains `Draft ready` and not yet approved --
    confirming or correcting a typo before approving is a legitimate, repeatable action. Refused
    (`InvalidCaseStateError`) once an `approval_granted` `Event` already exists, since an approval
    decision made against one address must never silently apply to a since-changed one (R1).
    """
    case = _draft_ready_case(session, case_id)
    answer = recipient_email.strip()
    if not answer or "@" not in answer:
        raise InvalidInputError("recipient address must be non-empty and contain '@'")
    matched_suggestion = _latest_proposed_recipient(session, case_id) == answer
    if not _update_recipient_if_not_approved(session, case_id, answer):
        session.rollback()
        raise InvalidCaseStateError(case_id, case.state, "eligible for a recipient change")
    session.add(
        create_event(
            case_id=case_id,
            kind="recipient_confirmed",
            actor="human",
            detail={"recipient": answer, "matched_suggestion": matched_suggestion},
        )
    )
    session.commit()
    session.refresh(case)
    return case


def _discovered_case(session: Session, case_id: int) -> Case:
    """The `Case` for `case_id`, if (and only if) it's still `Discovered` -- the window
    `set_case_jurisdiction()`/`set_case_drafting_language()` require: once `agent_loop._classify()`
    resolves and locks these fields in for a round, per its own docstring, they're settled and no
    longer meant to be edited out from under an already-drafted case."""
    case = _get_case(session, case_id)
    if case.state != CaseState.DISCOVERED:
        raise InvalidCaseStateError(case_id, case.state, "in 'Discovered' state")
    return case


def set_case_jurisdiction(session: Session, case_id: int, jurisdiction: str) -> Case:
    """Set `Case.applicable_jurisdiction` for a still-`Discovered` case and record a
    `jurisdiction_overridden` `Event`.

    Only reachable before `_classify()` ever reads/locks this field in (`_discovered_case()`'s
    gate) -- issue #43's manual-override intent, now reached via an explicit command instead of a
    blocking creation-time prompt (see `cli.prompt_case_overrides()`'s removal). Distinct `Event`
    kind from the agent's own `jurisdiction_proposed` (`cli._propose_jurisdiction()`) so the audit
    trail can always tell an auto-applied detector suggestion apart from a human's explicit
    override -- R22: jurisdiction/drafting-language are independent axes, never a package deal, so
    this has no `set_case_drafting_language()` counterpart call bundled in.
    """
    case = _discovered_case(session, case_id)
    answer = jurisdiction.strip()
    if not answer:
        raise InvalidInputError("jurisdiction must be non-empty")
    case.applicable_jurisdiction = answer
    session.add(case)
    session.add(
        create_event(
            case_id=case_id,
            kind="jurisdiction_overridden",
            actor="human",
            detail={"value": answer},
        )
    )
    session.commit()
    session.refresh(case)
    return case


def set_case_drafting_language(session: Session, case_id: int, drafting_language: str) -> Case:
    """Set `Case.drafting_language` for a still-`Discovered` case and record a
    `drafting_language_overridden` `Event`. See `set_case_jurisdiction()`'s docstring -- same gate,
    same audit-trail rationale, deliberately independent (R22)."""
    case = _discovered_case(session, case_id)
    answer = drafting_language.strip()
    if not answer:
        raise InvalidInputError("drafting language must be non-empty")
    case.drafting_language = answer
    session.add(case)
    session.add(
        create_event(
            case_id=case_id,
            kind="drafting_language_overridden",
            actor="human",
            detail={"value": answer},
        )
    )
    session.commit()
    session.refresh(case)
    return case


def approve_draft(session: Session, case_id: int) -> Case:
    """Record an `approval_granted` `Event` for a case eligible to send (`_send_eligible_case()` --
    `Draft ready` and not yet approved, or `Requires human action` with a pending preview draft) --
    never itself sends. The recurring loop's next pass finds this `Event`
    (`agent_loop._existing_approval_granted_event()`) and proceeds straight to the actual send
    (R1: this only records a decision).

    **The one moment `case.state` actually changes for the new `Requires human action` origin**
    (the "drafting is a preview, not a transition" redesign -- see `followup.py`'s module
    docstring): if the resolved case is currently `Requires human action` (a preview draft was
    composed via `followup.draft_additional_info_response()` but the case never left that state),
    this also flips `case.state = Draft ready` as part of this same write, immediately alongside
    the `approval_granted` `Event` -- the terminal "send it" decision, not a review step. After
    this, the existing, unmodified recurring loop (`cli.run_once()`'s generic "send every approved
    `Draft ready` case" pass) picks it up and sends exactly as it already does for a first-time
    draft -- no changes needed there. If the resolved case was already `Draft ready` (the original
    origin), this is a no-op on `case.state`, since it's already there.

    Known, accepted gap (OCR review finding): the `_send_eligible_case()` state check and the
    writes below are two separate operations, not one atomic statement (unlike
    `confirm_recipient()`'s `_update_recipient_if_not_approved()`) -- two genuinely concurrent
    approvals for the same case could both pass the check and each insert their own
    `approval_granted` event. This never risks a double send: `_existing_approval_granted_event()`
    only ever reads the single latest one, and the first real send immediately flips `case.state`
    away from `Draft ready`. The only cost is a harmless dangling duplicate event in the audit
    trail. Not worth an atomic-UPDATE-style rewrite (or a row lock) for a single-operator tool
    where two genuinely concurrent approval clicks for the same case are not a realistic scenario.
    """
    case = _send_eligible_case(session, case_id)
    if case.state == CaseState.REQUIRES_HUMAN_ACTION:
        case.state = CaseState.DRAFT_READY
        session.add(case)
    session.add(
        create_event(
            case_id=case_id,
            kind="approval_granted",
            actor="human",
            detail={"action": "send_request"},
        )
    )
    session.commit()
    session.refresh(case)
    return case


def reject_draft(session: Session, case_id: int) -> Case:
    """Reject a case eligible to send (`_send_eligible_case()` -- `Draft ready` and not yet
    approved, or `Requires human action` with a pending preview draft) -- immediate and final:
    `state` moves to `Rejected/Escalated` right here regardless of which origin it came from, since
    there's no further step for anything to take on a rejected case."""
    case = _send_eligible_case(session, case_id)
    case.state = CaseState.REJECTED_ESCALATED
    session.add(case)
    session.add(
        create_event(
            case_id=case_id,
            kind="approval_rejected",
            actor="human",
            detail={"action": "send_request"},
        )
    )
    session.commit()
    session.refresh(case)
    return case


def edit_draft(session: Session, case_id: int, body: str, *, subject: str | None = None) -> Case:
    """Hand-edit the current round's drafted body text (LLM-drafted-replies extension): requires
    a case eligible to send (`_send_eligible_case()` -- `Draft ready` and not yet approved, or
    `Requires human action` with a pending preview draft) -- refuses once approved (R1: editing
    after approval would silently apply the edit to an already-authorized send, since
    `send_request()` re-reads the latest `draft_proposed` `Event` fresh at send time rather than
    snapshotting it at approval time).

    Also now usable to tweak the LLM's composed reply for an additional-info round while the case
    is still nominally `Requires human action`, before ever clicking Send (the "drafting is a
    preview, not a transition" redesign -- see `followup.py`'s module docstring) -- not just for a
    `Draft ready` case as before.

    Records a new `draft_proposed` `Event` holding the edited body verbatim -- `subject` defaults to
    whatever `latest_draft()` currently has if not given, so editing only the body never silently
    blanks/changes the subject. Does not touch `case.state` either way -- stays `Draft ready` for
    the original origin, stays `Requires human action` for the new one.

    **This module is DB-only (see module docstring): `edit_draft()` never calls an LLM.** It records
    exactly what the human typed, character for character -- the LLM-drafting step
    (`agent_loop._compose_followup_reply()`) only ever runs once, when a resend is first composed
    from a human's notes; a human editing that draft afterward is editing the actual text that will
    be sent, not writing a fresh prompt for another draft.

    **Check-then-act race (OCR review finding, R1):** the state/approval check above and the
    `Event` write below used to be two separate round-trips with a real gap between them -- this
    app's kanban routes run in a worker thread pool (see `api/document.py`'s "plain def, not
    async def" rationale), each with its own `Session`/SQLite connection, so a concurrent
    `approve_draft()` call genuinely can interleave between them, not just in a multi-process
    deployment this single-operator tool doesn't have. If it did, the edited text would reach
    `send_request()` under an approval a human granted for the *unedited* draft -- exactly what
    R1 forbids, and worse than
    `approve_draft()`'s own already-accepted check-then-act gap (that one only risks a harmless
    duplicate `approval_granted` event; this one risks the *content* diverging from what was
    approved). `select_documents_for_resend()` has the identical shape (a bare `Event` write after
    the same gate) and gets the identical fix below. `attach_document()` deliberately does *not* --
    see its own docstring for why (it also writes an encrypted file to disk before the gate can be
    re-checked, so the same rollback-on-race approach would risk orphaning that file rather than
    just discarding an in-memory `Event`; fixing it safely needs a secure-delete-on-rollback path
    this change doesn't add, so it's left as a narrower, documented, accepted residual gap rather
    than a rushed fix with its own new failure mode).

    Fixed by re-running the approval check once more immediately after flushing (not yet
    committing) the new `Event`, rolling back if an approval slipped in during the gap. This isn't
    a full `SELECT ... FOR UPDATE`-style fix (SQLite has no row-level locking to ask for -- unlike
    Postgres, `with_for_update()` is a silent no-op on this project's actual database backend, so
    that pattern -- `confirm_recipient()`'s atomic `UPDATE ... WHERE` uses a *different* mechanism,
    an atomic statement, not a lock -- wouldn't actually close anything here). Instead it leans on
    a fact specific to SQLite: `session.flush()` is what actually sends the `INSERT` and, in doing
    so, takes this connection's write lock -- SQLite allows only one writer at a time regardless of
    journal mode, so a concurrent `approve_draft()` attempting its own `INSERT` from here on blocks
    until this transaction commits or rolls back, and can only be observed by the re-check below if
    it fully committed *before* this flush. This shrinks the race window from "the whole function
    call" down to "the handful of pure-Python lines between the first check above and this flush,
    with no I/O in between" -- not zero, but no longer a realistic window for a human's two
    near-simultaneous clicks to land in, which is the same practical bar `approve_draft()`'s
    documented gap is already held to.
    """
    case = _send_eligible_case(session, case_id)
    answer = body.strip()
    if not answer:
        raise InvalidInputError("draft body must be non-empty")
    if len(answer) > _MAX_TEXT_RESPONSE_CHARS:
        raise InvalidInputError(f"draft body must be at most {_MAX_TEXT_RESPONSE_CHARS} characters")

    current_subject, _ = latest_draft(session, case_id)
    session.add(
        create_event(
            case_id=case_id,
            kind="draft_proposed",
            actor="human",
            detail={
                "draft": answer,
                "subject": (subject.strip() if subject and subject.strip() else current_subject),
                "jurisdiction": case.applicable_jurisdiction or "",
                "drafting_language": case.drafting_language or "",
            },
        )
    )
    # Flush (not commit) first, then re-check -- see the docstring's "Check-then-act race" section
    # for why this ordering is what actually closes most of the gap on SQLite specifically.
    session.flush()
    if _has_unconsumed_approval_event(session, case_id):
        session.rollback()
        raise InvalidCaseStateError(
            case_id,
            case.state,
            "eligible to send/reject/edit ('Draft ready' and not yet approved, or 'Requires human "
            "action' with a pending draft)",
        )
    session.commit()
    session.refresh(case)
    return case


def _latest_unconsumed_classification(session: Session, case_id: int) -> Event | None:
    """The most recent `response_classified` `Event` for `case_id`, unless a `followup_confirmed`/
    `followup_disputed` `Event` already exists after it -- in which case it's already been acted on
    and this returns `None`. Mirrors `agent_loop._existing_approval_granted_event()`'s staleness
    shape: a case can cycle through a classified reply more than once over its lifetime, so only
    the most recent classification, if not yet consumed, is ever actionable."""
    classified = sorted(
        session.exec(
            select(Event).where(Event.case_id == case_id, Event.kind == "response_classified")
        ).all(),
        key=lambda event: event.id or 0,
    )
    if not classified:
        return None
    latest = classified[-1]
    # Filtered by kind at the SQL level (OCR review finding), not loaded-then-filtered-in-Python
    # like an earlier version of this function did -- mirrors `agent_loop.
    # _existing_approval_granted_event()`'s own "query only the specific kinds needed" shape.
    consumed_kinds = ("followup_confirmed", "followup_disputed")
    already_consumed = any(
        (event.id or 0) > (latest.id or 0)
        for event in session.exec(
            select(Event).where(Event.case_id == case_id, col(Event.kind).in_(consumed_kinds))
        ).all()
    )
    return None if already_consumed else latest


def _pending_followup_decision(session: Session, case_id: int) -> Event:
    """The pending, unconsumed `response_classified` `Event` for `case_id`, if its intent is
    `REJECTION` (the only intent a *human* decision applies to now) -- `InvalidCaseStateError`
    otherwise (nothing to confirm/dispute yet, or the case isn't even in `Awaiting response`).

    `CONFIRMATION` was dropped from the actionable set (maintainer-directed narrowing of issue #68
    -- see `followup.py`'s module docstring): `follow_up()` now resolves a `CONFIRMATION`
    classification immediately, itself, so a case is never left sitting in `Awaiting response`
    with a pending confirmation decision for a human to act on here -- only a pending `REJECTION`
    ever reaches this function's caller."""
    case = _get_case(session, case_id)
    if case.state != CaseState.AWAITING_RESPONSE:
        raise InvalidCaseStateError(case_id, case.state, "in 'Awaiting response' state")
    classification = _latest_unconsumed_classification(session, case_id)
    actionable_intents = (ReplyIntent.REJECTION.value,)
    if classification is None or classification.detail.get("intent") not in actionable_intents:
        raise InvalidCaseStateError(
            case_id, case.state, "awaiting a pending REJECTION follow-up decision"
        )
    return classification


def confirm_followup(session: Session, case_id: int) -> Case:
    """Confirm a classified reply's outcome -- moves the case to `Rejected/Escalated`. The
    follow-up analogue of `approve_draft()`: applies `agent_loop.
    apply_confirmed_followup_transition()`'s guard, which refuses unless a persisted
    `followup_confirmed` `Event` for this exact case backs the transition.

    Always a `REJECTION` in practice, not branched on here (code-reviewer follow-up on this PR):
    `_pending_followup_decision()`'s `actionable_intents` (maintainer-directed narrowing of issue
    #68 -- see `followup.py`'s module docstring) never returns a `CONFIRMATION` classification
    anymore, since `follow_up()` now resolves one itself before a case ever reaches `Awaiting
    response` with a pending confirmation decision. Checked explicitly here, rather than branched
    on, to keep that guarantee visible at the one call site that would otherwise need to silently
    trust it -- a raised `InvalidCaseStateError`, not a bare `assert` (OCR review finding: an
    `assert` is stripped under `python -O`, silently letting a future regression or race through
    instead of refusing)."""
    case = _get_case(session, case_id)
    classification = _pending_followup_decision(session, case_id)
    intent = classification.detail["intent"]
    if intent != ReplyIntent.REJECTION.value:
        raise InvalidCaseStateError(
            case_id, case.state, f"awaiting a pending REJECTION follow-up decision (got {intent!r})"
        )
    confirmation_event = create_event(
        case_id=case_id,
        kind="followup_confirmed",
        actor="human",
        detail={"action": "confirm_rejection", "target_state": CaseState.REJECTED_ESCALATED.value},
    )
    session.add(confirmation_event)
    session.flush()
    apply_confirmed_followup_transition(
        session, case, confirmation_event, CaseState.REJECTED_ESCALATED
    )
    session.commit()
    session.refresh(case)
    return case


def dispute_followup(session: Session, case_id: int) -> Case:
    """Dispute a classified reply's outcome -- the human disagrees with the agent's reading, so the
    case moves to `Requires human action` instead of any terminal state (never auto-closed on a
    disputed classification). Always a `REJECTION` in practice -- see `confirm_followup()`'s
    docstring for why this is checked explicitly (raising, not a bare `assert`) rather than
    branched on."""
    case = _get_case(session, case_id)
    classification = _pending_followup_decision(session, case_id)
    intent = classification.detail["intent"]
    if intent != ReplyIntent.REJECTION.value:
        raise InvalidCaseStateError(
            case_id, case.state, f"awaiting a pending REJECTION follow-up decision (got {intent!r})"
        )
    session.add(
        create_event(
            case_id=case_id,
            kind="followup_disputed",
            actor="human",
            detail={
                "action": "confirm_rejection",
                "proposed_target_state": CaseState.REJECTED_ESCALATED.value,
            },
        )
    )
    case.state = CaseState.REQUIRES_HUMAN_ACTION
    session.add(case)
    session.commit()
    session.refresh(case)
    return case


def reopen_case(session: Session, case_id: int) -> Case:
    """Move a `Deletion confirmed` case back to `Requires human action` -- the human undo for a
    wrongly agent-auto-closed `CONFIRMATION` (maintainer-directed narrowing of issue #68's
    original human-gate-before-close guarantee; see `followup.py`'s module docstring, "Why
    CONFIRMATION auto-closes now"). Same "it's your own case, your call" posture as
    `delete_vault_document()`/`rename_vault_document()` above: no extra gate beyond the case
    actually being in `Deletion confirmed` (`InvalidCaseStateError` otherwise -- there is nothing
    to undo for a case that was never auto-closed, or that closed via a different, already
    human-reviewed path like a confirmed `REJECTION`'s `Rejected/Escalated`, which this action
    deliberately does not cover -- reopening a rejection is out of this action's scope per the
    maintainer's decision).

    Records a `case_reopened` `Event` (actor=`"human"` -- this is always a human-triggered
    action, unlike `followup_confirmed`, which can be agent- or human-authored) before mutating
    `state`, mirroring this module's audit-first convention elsewhere. `detail` carries both
    `from_state`/`to_state` (OCR review finding), the same self-describing shape `followup_
    confirmed`'s `target_state`/`followup_disputed`'s `proposed_target_state` already use -- so
    the transition is readable straight off the event trail, not just inferable from `Case.state`
    at two different points in time."""
    case = _get_case(session, case_id)
    if case.state != CaseState.DELETION_CONFIRMED:
        raise InvalidCaseStateError(case_id, case.state, "in 'Deletion confirmed' state")
    session.add(
        create_event(
            case_id=case_id,
            kind="case_reopened",
            actor="human",
            detail={
                "from_state": CaseState.DELETION_CONFIRMED.value,
                "to_state": CaseState.REQUIRES_HUMAN_ACTION.value,
            },
        )
    )
    case.state = CaseState.REQUIRES_HUMAN_ACTION
    session.add(case)
    session.commit()
    session.refresh(case)
    return case


def _requires_human_action_case(session: Session, case_id: int) -> Case:
    case = _get_case(session, case_id)
    if case.state != CaseState.REQUIRES_HUMAN_ACTION:
        raise InvalidCaseStateError(case_id, case.state, "in 'Requires human action' state")
    return case


def _has_unconsumed_approval_event(session: Session, case_id: int) -> bool:
    """True if `case_id`'s latest `approval_granted` `Event` has no later `request_sent` `Event`
    after it -- i.e. it's still pending, not an earlier round's already-consumed approval.

    Mirrors `agent_loop._existing_approval_granted_event()`'s exact staleness shape, duplicated
    here (a plain query, not a private cross-module import) rather than importing that other
    module's private helper -- `case_actions.py`'s own module docstring is explicit that it only
    ever reads/writes `Event`/`Case` state itself, not delegates its gating logic elsewhere.
    """
    approval_events = session.exec(
        select(Event).where(Event.case_id == case_id, Event.kind == "approval_granted")
    ).all()
    if not approval_events:
        return False
    latest_approval = max(approval_events, key=lambda e: e.id or 0)
    sent_events = session.exec(
        select(Event).where(Event.case_id == case_id, Event.kind == "request_sent")
    ).all()
    if sent_events:
        latest_sent = max(sent_events, key=lambda e: e.id or 0)
        if (latest_approval.id or 0) < (latest_sent.id or 0):
            return False  # already consumed by an earlier round's send
    return True


def _editable_round_case(session: Session, case_id: int) -> Case:
    """The `Case` for `case_id`, if it's in an "editable round" -- `Requires human action` (no
    draft attempted yet this round), or `Draft ready` with no unconsumed `approval_granted` `Event`
    yet (a draft exists, but hasn't been authorized to send). `InvalidCaseStateError` otherwise.

    Shared by `select_documents_for_resend()`/`attach_document()` (so a document can be selected
    or uploaded either before a draft exists or while reviewing/editing one) and `edit_draft()`
    (which additionally requires the `Draft ready` half of this exact same condition -- there is no
    draft to edit yet in `Requires human action`). The `Draft ready`-and-not-yet-approved half is
    the R1 safety line: editing the draft or attaching a new document *after* approval would
    silently apply to an already-authorized send, since `send_request()` re-reads both fresh on
    every pass rather than snapshotting them at approval time.
    """
    case = _get_case(session, case_id)
    if case.state == CaseState.REQUIRES_HUMAN_ACTION:
        return case
    if case.state == CaseState.DRAFT_READY and not _has_unconsumed_approval_event(session, case_id):
        return case
    raise InvalidCaseStateError(
        case_id,
        case.state,
        "editable ('Requires human action', or 'Draft ready' and not yet approved)",
    )


_MAX_TEXT_RESPONSE_CHARS = 10_000
"""Upper bound on a human-supplied text answer's length (OCR review finding) -- matches the
kanban form's own `maxlength` (defense-in-depth: the CLI's `provide-text` subcommand has no HTML
form to enforce it client-side, so the real limit has to live here). Generous for a genuine
"here's the info the site asked for" answer; mainly a backstop against an accidental paste of
something enormous ending up verbatim in the audit trail (`Event.detail`, a JSON column with no
size limit of its own)."""


def provide_text_response(session: Session, case_id: int, text: str) -> Case:
    """Record a human-supplied text answer for a `Requires human action` case. Records only -- the
    human's own later, explicit "Draft" action (`followup.draft_additional_info_response()`, kanban
    or CLI) is what turns this into an actual preview draft, not this function."""
    case = _requires_human_action_case(session, case_id)
    answer = text.strip()
    if not answer:
        raise InvalidInputError("text response must be non-empty")
    if len(answer) > _MAX_TEXT_RESPONSE_CHARS:
        raise InvalidInputError(
            f"text response must be at most {_MAX_TEXT_RESPONSE_CHARS} characters"
        )
    session.add(
        create_event(
            case_id=case_id,
            kind="additional_info_text_provided",
            actor="human",
            detail={"text": answer},
        )
    )
    session.commit()
    session.refresh(case)
    return case


def attach_document(
    session: Session, case_id: int, content: bytes, mime_type: str, *, label: str | None = None
) -> Case:
    """Record a human-supplied document answer for a case in an editable round (`Requires human
    action`, or `Draft ready` and not yet approved -- `_editable_round_case()`), via the same
    encrypted-at-rest lifecycle every other document channel uses (`document_store.
    store_document()`, already channel-agnostic). Records only -- see `provide_text_response()`'s
    docstring for why the actual draft/resend is a separate, explicit human action, not this
    function's.

    Issue #111 (document vault): the uploaded document goes into the vault (reusable afterward, not
    consumed by this one round) and is immediately selected for `case_id`'s current round via
    `select_documents_for_resend()` -- this keeps this function's own external behavior identical
    to before #111 (upload once, it's what the next resend uses), while also leaving the document
    available for a human to explicitly re-select for a *future* round or case via
    `list_vault_documents()`.

    Also callable while `Draft ready` (LLM-drafted-reply extension): a human reviewing an already-
    composed draft can still attach a document before approving -- `cli.run_once()`'s send loop
    already re-derives `followup.selected_documents()` fresh from events on every pass (not baked
    in at draft-composition time), so a document selected after the draft was composed is picked up
    automatically once approved, with no changes needed at the send layer.

    **Check-then-act race (OCR review finding, R1) -- deliberately *not* given the same
    flush-then-recheck fix `edit_draft()`/`select_documents_for_resend()` get.** This function has
    the same underlying gap (the `_editable_round_case()` gate check and the writes below can race
    against a concurrent `approve_draft()`), but closing it the same way would mean rolling back
    *after* `document_store.store_document()` has already written an encrypted file to disk --
    `session.rollback()` only undoes the DB row, not that file, which would then need a proper
    secure-delete (overwrite + unlink, per this project's non-negotiable retention rule -- see
    `CLAUDE.md`) on the race path specifically, not just a bare `unlink()`. That's real, scoped
    work this change doesn't add rather than something to bolt on hastily; left as a narrower,
    documented, accepted residual gap for now (same bar `approve_draft()`'s own accepted
    check-then-act gap is already held to), not silently ignored. A future fix should add a
    public secure-delete helper to `document_store.py` this function can call on the rollback
    path, then apply the identical flush-then-recheck pattern.
    """
    case = _editable_round_case(session, case_id)
    if not content:
        raise InvalidInputError("document content must be non-empty")
    document = document_store.store_document(session, case, content, mime_type, label=label)
    if document.id is None:
        # Explicit check, not `assert` (OCR review finding): `assert` is stripped under
        # `python -O`, silently letting a broken invariant through in production instead of
        # failing loud -- matches this codebase's established convention (see e.g.
        # `agent_loop._execute_send()`'s identical rationale for its own document.id check).
        raise RuntimeError(
            f"document for case {case_id} has not been persisted (no id) -- "
            "store_document() must flush before this point"
        )
    _record_document_selection(session, case_id, [document.id])
    session.commit()
    session.refresh(case)
    return case


def select_documents_for_resend(
    session: Session, case_id: int, document_ids: Sequence[int]
) -> Case:
    """Select one or more existing vault documents (`document_store.list_vault_documents()`) to
    attach to `case_id`'s current editable round (`Requires human action`, or `Draft ready` and not
    yet approved -- `_editable_round_case()`), without re-uploading anything -- the explicit
    counterpart of `attach_document()`'s upload-and-select-in-one-step, for reusing a document
    already in the vault (issue #111).

    Records only, same shape as `provide_text_response()`/`attach_document()` -- the human's own
    later, explicit "Draft" action (`followup.draft_additional_info_response()`, kanban or CLI), or
    `cli.run_once()`'s send loop directly if a draft already exists -- see `attach_document()`'s
    docstring -- is what turns this into an actual draft/attachment. Every id must name a document
    that currently exists and
    is still `DocumentStatus.AVAILABLE` (`InvalidInputError` naming any that aren't) -- deliberately
    not scoped to documents originally uploaded *on* `case_id`: reusing a document across cases is
    exactly issue #111's point (see `Document`'s own docstring for why no schema change was needed
    for that).

    **Check-then-act race (OCR review finding, R1) -- same fix as `edit_draft()`:** this has the
    identical shape (`_editable_round_case()`'s gate check, then a bare `Event` write, with a real
    gap between them across this app's worker-thread-pool routes) and risks the same outcome -- a
    document selected concurrently with a fresh approval would otherwise be attached to a send the
    human never actually reviewed alongside their approval decision. See `edit_draft()`'s docstring
    for the full "why flush-then-recheck closes most of this on SQLite specifically" reasoning;
    applied identically here since this function's write is also a plain `Event` add, with no file
    I/O of its own to complicate a rollback (unlike `attach_document()`).
    """
    case = _editable_round_case(session, case_id)
    if not document_ids:
        raise InvalidInputError("must select at least one document")
    # Deduplicated, order preserved (OCR review finding): a client sending the same id twice (a
    # double-submitted checkbox, a hand-typed CLI list with a repeat) would otherwise attach the
    # same document twice in one email -- `dict.fromkeys()` is the standard "unique, keep first
    # occurrence's position" idiom.
    unique_ids = list(dict.fromkeys(document_ids))
    # One batch query instead of one `session.get()` per id (OCR review finding) -- same `.in_()`
    # pattern `agent_loop.py` already uses for a batch document lookup.
    found_by_id = {
        document.id: document
        for document in session.exec(select(Document).where(col(Document.id).in_(unique_ids))).all()
    }
    unusable: list[int] = []
    for document_id in unique_ids:
        document = found_by_id.get(document_id)
        if document is None or document.status is not DocumentStatus.AVAILABLE:
            unusable.append(document_id)
    if unusable:
        raise InvalidInputError(
            f"document id(s) {sorted(unusable)} do not exist or are not available in the vault"
        )
    _record_document_selection(session, case_id, unique_ids)
    # Flush (takes the write lock on SQLite), then re-check before committing -- see
    # edit_draft()'s docstring for the full reasoning.
    session.flush()
    if _has_unconsumed_approval_event(session, case_id):
        session.rollback()
        raise InvalidCaseStateError(
            case_id,
            case.state,
            "editable ('Requires human action', or 'Draft ready' and not yet approved)",
        )
    session.commit()
    session.refresh(case)
    return case


def _record_document_selection(session: Session, case_id: int, document_ids: list[int]) -> None:
    """Shared by `attach_document()`/`select_documents_for_resend()`: record which vault document
    ids are selected for `case_id`'s current round. Plain integer ids only, never document content
    -- not one of `DOCUMENT_EVENT_KINDS` (see `core.models.validate_document_event_detail()`), so
    no special `detail` shape restriction applies, same as any other event's `detail`."""
    session.add(
        create_event(
            case_id=case_id,
            kind="documents_selected_for_resend",
            actor="human",
            detail={"document_ids": document_ids},
        )
    )


def list_vault_documents(session: Session) -> list[Document]:
    """Every document currently in the vault (`DocumentStatus.AVAILABLE`), most recently uploaded
    first -- for rendering a selection UI (CLI `list-documents`, kanban `GET /vault`).

    Ordered and filtered by the database itself, not `sorted()` in Python -- `col(Document.id)`
    (not a bare `Document.id`) is what lets `ty` accept `.order_by()` against a nullable-until-
    flushed `int | None` column descriptor (same reason `_update_recipient_if_not_approved()` above
    and `agent_loop._thread_reference_ids()` already reach for `col(...)`, applied here to
    `.order_by()` instead of `.where()`/`.in_()`)."""
    return list(
        session.exec(
            select(Document)
            .where(Document.status == DocumentStatus.AVAILABLE)
            .order_by(col(Document.id).desc())
        ).all()
    )


def delete_vault_document(session: Session, document_id: int) -> None:
    """Permanently remove a document from the vault (secure-delete + `PURGED`) -- always allowed,
    not scoped to any case's round: deleting your own stored document is the human's call to make
    at any time, not gated on any case being in a particular state."""
    document = session.get(Document, document_id)
    if document is None or document.status is not DocumentStatus.AVAILABLE:
        raise InvalidInputError(f"document {document_id} does not exist or is not in the vault")
    document_store.delete_vault_document(session, document)
    session.commit()


def rename_vault_document(session: Session, document_id: int, label: str) -> Document:
    """Set/replace `document_id`'s label -- same "it's your own document, always allowed" reasoning
    as `delete_vault_document()` above: no case-state gate, since renaming isn't scoped to any
    case's round either. Rejects an empty/whitespace-only label (`InvalidInputError`) so a document
    can't be renamed back into the un-informative blank state the `document-{id}` placeholder
    already exists to cover -- callers that want to *clear* a label back to that placeholder should
    delete and re-upload, not rename to blank.

    Records a `document_renamed` `Event` (code-reviewer follow-up on this PR): `delete_vault_
    document()` already records `document_purged`, so a rename that silently updated the row with
    no audit trail would be the one vault-document mutation that left no trace of what changed --
    `detail` carries the old/new label text (not content, so no CLAUDE.md "audit log: hash +
    metadata only, never content" conflict -- a label is metadata a human typed, same as any other
    field already recorded verbatim elsewhere, e.g. `additional_info_text_provided`)."""
    document = session.get(Document, document_id)
    if document is None or document.status is not DocumentStatus.AVAILABLE:
        raise InvalidInputError(f"document {document_id} does not exist or is not in the vault")
    label = label.strip()
    if not label:
        raise InvalidInputError("label must be non-empty")
    previous_label = document.label
    document.label = label
    session.add(document)
    session.add(
        create_event(
            case_id=document.case_id,
            kind="document_renamed",
            actor="human",
            detail={"previous_label": previous_label, "label": label},
        )
    )
    session.commit()
    session.refresh(document)
    return document
