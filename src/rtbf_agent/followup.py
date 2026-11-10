"""Follow-up orchestration: monitor a `Case` for a reply, classify it, transition on confirmation.

The composition layer of the "Followup" component (issue #68, spec §3.1), sibling to `agent_loop`
and `cli`. It wires three already-tested-in-isolation pieces together for one `Case`:

- `plugins/followup/monitor.InboxReplyMonitor` -- finds the oldest new reply to the case (or
  reports `None`), over a read-only `MailboxReader`.
- `plugins/followup/detector.ResponseClassifier` -- reads that reply's intent with a real,
  decision-driving LLM call (or reports `None` for unclear/silence).
- `agent_loop`'s follow-up transitions -- the guarded, audit-first `Case.state` changes a
  classification produces, with terminal closes gated on an explicit human confirmation.

## What one `follow_up()` pass does, and does *not*, do

For a `Case` in `Awaiting response` (`FOLLOWUP_MONITORED_STATES`):

1. Look for a new reply arriving after the case's `request_sent` event, not already processed.
2. If none: **return the case completely untouched** -- no state change, no `Event`, no
   notification, no deadline (R9). This is the safety-critical path: monitoring is purely reactive.
3. If a reply is found: classify it, and record a `response_classified` `Event` **whatever the
   outcome** (R3 -- the proposal is audited even when the human later disagrees, and the event's
   `message_id` is what marks the reply processed so it is never classified twice).
4. Act on the classified intent:
   - **unclear / silence** (`classify()` returned `None`): record the audit event and stop -- a
     reply arrived but carries no interpretable signal, so nothing transitions (the spec's
     "silencio" outcome; still not a timeout or escalation).
   - **additional-info request**: hand off to `Requires human action` and notify -- no confirmation
     needed (issue #69's provide-and-resend loop, driven by `case_actions.
     provide_text_response()`/`attach_document()`/`select_documents_for_resend()` to record a
     round's answer, and the human's explicit, synchronous "Draft" action
     (`draft_additional_info_response()` below, issue #117/#119 superseded by a later maintainer
     decision -- see "Why drafting is now an explicit, synchronous action" below) to compose a
     previewable reply from it. Never a blocking wait here, and never an automatic background
     poll either -- see that section for why the old poll-based design was retired.
   - **confirmation**: closes the case immediately as `Deletion confirmed` -- a maintainer-directed
     narrowing of issue #68's original guarantee (see this module's own docstring section below,
     "Why CONFIRMATION auto-closes now"). This function records a `followup_confirmed` `Event`
     itself (actor `"agent"`) and calls `agent_loop.apply_confirmed_followup_transition()` --
     the exact same guarded transition `case_actions.confirm_followup()` uses -- so the audit
     trail and the terminal-transition code path both stay singular whether the confirming actor
     is a human or the agent. The notification sent afterward says the case *was* closed, and
     that a human can undo it via the "reopen" action (`case_actions.reopen_case()`) if the
     classification was wrong.
   - **rejection**: unchanged -- send a one-way notice only (`Notifier.notify()`); this function
     never itself transitions to a terminal state for a rejection. A human confirms or disputes
     via `case_actions.confirm_followup()`/`dispute_followup()` (kanban or CLI) whenever they get
     to it; there is no blocking wait anywhere in this module (automated-case-loop rework).

## Why double-resolution (issue #67's concern) is safe here

Two things make a case impossible to resolve twice, even across repeated passes or two notifier
channels racing:

- **State-gated entry.** `follow_up()` (and every `agent_loop` transition it calls) does nothing
  unless the case is still in a monitored state. Once a confirmation moves it to a terminal state
  -- or a dispute/additional-info reply moves it to `Requires human action` -- a subsequent pass
  returns at the state guard.
- **Processed-id dedup, scoped globally, not per-case.** A classified reply's `message_id` is
  recorded on its `response_classified` event; `_processed_reply_message_ids()` excludes it from
  *every* case's next monitoring pass, not just the case it was originally classified for.
  Deliberately global rather than per-`Case`: `InboxReplyMonitor`'s matching heuristic keys on
  sender *domain* (see `plugins/followup/monitor.py`'s docstring), not a thread/`In-Reply-To`
  header, so two `Case`s that happen to share a recipient domain (e.g. a company with several
  regional/brand domains routing deletion replies through one centralized `privacy@`/ticketing
  address -- the `(domain, target_email)` dedup key in `create_cases_for_new_accounts()` does not
  prevent this, since it keys on the *case's own* domain, not its recipient's) could otherwise both
  match the exact same incoming message and each independently propose a transition from it --
  asking a human to confirm the same reply's outcome twice, once for a case it was never actually
  about. Scoping the dedup globally means only the first case a monitoring pass reaches ever claims
  a given message; every other case's pass sees it as already-processed and leaves that case
  untouched (still R9-safe -- no wrong auto-action, just no follow-up progress for it that pass, a
  human can always follow up on it manually via the recorded evidence). Combined with
  `agent_loop`'s persisted-`followup_confirmed` guard on the terminal transition itself, this is the
  same defense-in-depth that stops `agent_loop._execute_send()` firing twice for one approval.
- **The actual reply text is always shown to the human**, not just the LLM's paraphrase (see
  `_reply_excerpt()` below) -- surfaced in every confirmation/notification this module sends, so a
  human reviewing a proposal (confirmation, rejection, or an additional-info hand-off) has enough of
  the primary source to notice if a reply doesn't actually seem to be about this case, not just the
  agent's own account of it.

## Why CONFIRMATION auto-closes now (maintainer-directed narrowing, not an oversight)

This module's original design (issue #68) made it structurally impossible for *any* path to close
a case without a preceding human confirmation -- "a case can never be closed... by any path that
skipped a human's explicit confirmation." A `CONFIRMATION` classification is now an explicit,
deliberate exception to that: when the LLM classifies a reply as a clear statement that the
deletion is done, `follow_up()` closes the case itself, live, with no separate human click. This
was an explicit maintainer decision (not a regression slipped in under this docstring's older
claim), made because the manual-confirm step for `CONFIRMATION` had turned out to add friction
without adding real safety -- a human reviewing "the site says it's done, please click confirm" was
already trusting the LLM's reading before pressing the button in the overwhelming majority of
cases, so the button was mostly ceremony, not a genuine additional check.

The property actually preserved is **auditability, not pre-approval**: every terminal transition,
whichever path triggers it, is still backed by a persisted `followup_confirmed` `Event` (agent-
authored here, human-authored via `case_actions.confirm_followup()` for a disputed-then-reconfirmed
case or a `REJECTION`) and still goes through the one guarded function,
`agent_loop.apply_confirmed_followup_transition()`, that refuses to flip `state` without one. What's
new is *who* is allowed to author that event for a `CONFIRMATION` -- the agent, not only a human.

The mitigating control for a wrong auto-close is `case_actions.reopen_case()`: a human who
disagrees with an auto-closed `Deletion confirmed` case can move it back to
`Requires human action` at any time, no time limit, exactly the same "it's your case, your call"
posture `delete_vault_document()`/`rename_vault_document()` already take toward vault documents.
This is a deliberately after-the-fact safety net (a human can always undo it) rather than the
original before-the-fact gate (a human must always approve it first) -- narrower than issue #68's
original guarantee, by explicit design, for `CONFIRMATION` only. `REJECTION` is untouched: a site
refusing or escalating a request still only closes via the existing manual confirm/dispute buttons,
since getting that call wrong has asymmetric downside (a case wrongly left open due to a disputed
rejection is recoverable; one may still want a human's eyes on an actual refusal before treating it
as final).

Known limitation, not fixed here: a reply from a third-party ticketing domain that never matches
the recipient's registrable domain (e.g. `company.zendesk.com` for a request sent to
`privacy@company.com`) is invisible to `InboxReplyMonitor` and the case sits in a monitored state
indefinitely with no signal that monitoring can't see it -- safe per R9 (nothing is auto-escalated),
but it silently defeats this component's purpose for that case. Left as a documented gap for a
follow-up rather than expanding this issue's scope; a human can still monitor and act on such a
case manually, exactly as before this issue existed.

## Why drafting is now an explicit, synchronous action (issue #117/#119, superseded)

Issue #117 fixed a real race (drafting the moment a document or note merely existed, before a human
was actually done with the round) by adding a separate "this round is ready" signal
(`additional_info_ready`, recorded by the now-retired `case_actions.
approve_additional_info_response()`) that a background pass, `check_for_pending_additional_info()`,
polled for -- and, once found, immediately drafted *and sent* in the same pass (a "fused approval",
see the retired `agent_loop.resend_with_additional_info()`'s own former docstring).

Live-testing that fix, the maintainer rejected the fused-approval half of it: clicking "ready"
composed a reply and sent it in the same breath, with no window to actually read the LLM's wording
first, and the only way to get a review window at all was for the case to hop to a separate `Draft
ready` kanban column first. What was wanted instead: write a note, attach documents, click a
**"Draft"** action, see the composed reply appear right there with the body and documents both
still editable, and only click **Send** once satisfied -- without the case ever appearing to change
kanban column in between drafting and sending.

That request makes the whole poll-based mechanism unnecessary, not just the fused-send half of it:
once drafting is a direct, explicit, synchronous human action (a button click / route call --
`draft_additional_info_response()` below), there is no more "has the human finished typing yet?"
race for a background loop to guess at, and nothing left for it to poll for. So
`check_for_pending_additional_info()`/`_has_fresh_additional_info_ready()`, the
`additional_info_ready` event kind, and `case_actions.approve_additional_info_response()` were all
removed rather than kept alongside the new explicit path -- retiring the mechanism, not layering a
second one under it. `draft_additional_info_response()` resolves the human's current fresh text/
document answer exactly the way the old poll used to (`selected_documents()`/
`_pending_text_response()`, unchanged) and calls `agent_loop.compose_additional_info_draft()`
synchronously -- the compose half of the old fused function, with the state change and the send
call both removed: `case.state` stays `Requires human action` through drafting, and only actually
sending (`case_actions.approve_draft()`, widened to also accept this origin -- see that function's
own docstring) ever flips it to `Draft ready`.
"""

from __future__ import annotations

from sqlmodel import Session, select

from rtbf_agent.agent_loop import (
    FOLLOWUP_MONITORED_STATES,
    apply_confirmed_followup_transition,
    compose_additional_info_draft,
    flag_requires_human_action,
)
from rtbf_agent.core.interfaces import LLMProvider, NotificationMessage, Notifier
from rtbf_agent.core.models import (
    Case,
    CaseState,
    Document,
    DocumentStatus,
    Event,
    create_event,
)
from rtbf_agent.plugins.followup.detector import ReplyIntent, ResponseClassifier
from rtbf_agent.plugins.followup.monitor import InboxReplyMonitor, MailboxMessage


def _latest_request_sent_event(session: Session, case_id: int) -> Event | None:
    """The case's most recent `request_sent` `Event`, or `None` if never sent.

    Two facts `follow_up()` needs both come from this one event: `.timestamp` (a reply is only a
    reply if it arrived *after* the request was sent -- `None` here means the case is not genuinely
    in a post-send state, so `follow_up()` does nothing, defensively, rather than guessing a send
    time) and `.detail.get("sent_message_id")` (the outbound `Message-ID`
    `agent_loop._execute_send()` generated, if this case was sent after that started being
    recorded -- lets `monitor.find_new_reply()` search by `In-Reply-To`/`References` instead of a
    broad sender-domain guess; `None` for a case sent before this existed, which falls back to the
    old domain-based search).
    """
    events = session.exec(
        select(Event).where(Event.case_id == case_id, Event.kind == "request_sent")
    ).all()
    if not events:
        return None
    # Latest wins; sorted in Python for the same `Event.id`-is-nullable typing reason
    # `agent_loop.latest_draft()` documents.
    return max(events, key=lambda event: event.timestamp)


_EXCERPT_BODY_CHARS = 300
"""How much of a reply's body to quote verbatim in a human-facing summary/notification. Enough to
let a human sanity-check the reply is actually about this case (not just take the LLM's paraphrase
on faith -- see this module's docstring), short enough to stay readable in a Telegram message."""


def _reply_excerpt(reply: MailboxMessage) -> str:
    """A short, human-readable quote of `reply`'s subject + body, for a confirmation/notification.

    Always the *actual* reply text, never only the LLM's own account of it -- a human confirming a
    proposed close, or acting on an additional-info hand-off, should be able to check the primary
    source, not just the agent's paraphrase (this matters most exactly when a reply might have been
    misattributed to the wrong case -- see the module docstring's cross-case dedup discussion)."""
    body = reply.body.strip()
    if len(body) > _EXCERPT_BODY_CHARS:
        body = body[:_EXCERPT_BODY_CHARS].rstrip() + "..."
    return f'Subject: "{reply.subject}"\n{body}'


def _processed_reply_message_ids(session: Session) -> frozenset[str]:
    """Every reply message id already classified for *any* case (the reprocessing guard).

    Deliberately global, not scoped to one `case_id`: `InboxReplyMonitor` matches on sender
    *domain*, not a thread/`In-Reply-To` header (see `plugins/followup/monitor.py`), so two `Case`s
    that share a recipient domain could otherwise both match the same incoming message. Scoping
    this set globally means whichever case's monitoring pass reaches a message first is the only one
    that ever classifies it -- every other case's pass sees it as already-processed and leaves that
    case untouched, rather than both independently proposing a transition from the same reply (see
    this module's docstring for the full reasoning). Every `response_classified` `Event`, across
    every case, records the `message_id` it classified; collecting them all here is what enforces
    that.

    A known, deliberately-unaddressed cost of the fix above: this is a full scan of every
    `response_classified` `Event` ever recorded, across every case, on every monitoring pass -- it
    grows unbounded with total follow-up history. Scoping it back to one `case_id`, or time-bounding
    it, would each reopen the exact misattribution risk this global scope exists to close (a
    time-bounded window could let a genuinely-already-processed old message fall outside the window
    and be wrongly reclassified as new). Left as a known scaling limitation rather than trading
    correctness for it -- a personal, single-operator tool's total follow-up event volume is not
    expected to reach a scale where this matters in practice; revisit with a proper index/schema
    migration if it ever does.
    """
    events = session.exec(select(Event).where(Event.kind == "response_classified")).all()
    ids = {
        event.detail.get("message_id")
        for event in events
        if isinstance(event.detail.get("message_id"), str)
    }
    return frozenset(message_id for message_id in ids if isinstance(message_id, str))


async def follow_up(
    session: Session,
    case: Case,
    monitor: InboxReplyMonitor,
    classifier: ResponseClassifier,
    notifier: Notifier,
) -> Case:
    """One monitor -> classify -> (human-confirmed) transition pass for one `Case`.

    Safe to call repeatedly on the same case (the follow-up counterpart of `run_cycle()`'s
    idempotence): a case with no new reply is left byte-for-byte unchanged (R9), and a reply already
    classified in an earlier pass is never reclassified. Returns `case` (possibly transitioned).

    Only acts on a case in `FOLLOWUP_MONITORED_STATES`; any other state is an immediate no-op, so a
    case already `Deletion confirmed`/`Rejected/Escalated`/`Requires human action`/etc. is never
    touched. Raises only on a genuine programmer error (an unpersisted case); a real
    `LLMProvider`/`MailboxReader` transport failure propagates from the collaborator (the CLI layer
    decides how to degrade, mirroring `_propose_jurisdiction`/`_propose_recipient`).

    Known, accepted limitation: two genuinely concurrent invocations of this function for the same
    case (e.g. two `python -m rtbf_agent` processes running at once -- not something a single
    sequential CLI run ever does to itself) could both read the same reply as "not yet processed"
    before either commits, each recording its own `response_classified` event for it. The actual
    safety property -- a case is never *terminally transitioned* twice -- still holds regardless:
    this function itself never transitions to a terminal state at all (that only ever happens via
    `case_actions.confirm_followup()`, a separate, explicitly human-triggered call whose own guard
    plus `apply_confirmed_followup_transition`'s persisted-Event check cover it), so the worst
    outcome of a double-classification here is a harmless duplicate audit event and wasted
    LLM/Gmail calls in that narrow window, not an incorrect state change. Not defended against with
    a DB-level uniqueness constraint or lock here -- disproportionate engineering for a personal,
    single-operator tool that isn't expected to run more than one instance at a time.
    """
    if case.state not in FOLLOWUP_MONITORED_STATES:
        return case
    if case.id is None:
        raise RuntimeError("case must be persisted (have an id) before it can be followed up")

    sent_event = _latest_request_sent_event(session, case.id)
    if sent_event is None:
        # No record of the request ever having been sent -- nothing to monitor for a reply *to*.
        # Leave the case untouched rather than inventing a baseline time (R9).
        return case
    sent_at = sent_event.timestamp
    sent_message_id = sent_event.detail.get("sent_message_id")

    already_processed = _processed_reply_message_ids(session)
    reply = await monitor.find_new_reply(
        case,
        sent_at=sent_at,
        already_processed_ids=already_processed,
        in_reply_to_message_id=sent_message_id if isinstance(sent_message_id, str) else None,
    )
    if reply is None:
        # The single most safety-critical branch (R9): a reply does not exist yet, so this pass
        # changes absolutely nothing about the case -- no state, no Event, no notification, no
        # deadline. Monitoring is purely additive.
        return case

    classification = await classifier.classify(
        site=case.site,
        domain=case.domain,
        reply_subject=reply.subject,
        reply_body=reply.body,
    )
    intent = classification.intent if classification is not None else None

    # Record the classification for audit (R3) *and* to mark this message processed, whatever the
    # outcome -- including "unclear", so an uninterpretable reply is never re-classified every pass.
    session.add(
        create_event(
            case_id=case.id,
            kind="response_classified",
            actor="agent",
            detail={
                "message_id": reply.message_id,
                "intent": intent.value if intent is not None else "unclear",
                "reasoning": classification.reasoning if classification is not None else "",
            },
        )
    )
    session.flush()  # persist the processed-id marker before any transition/commit

    if classification is None:
        # Unclear / silence: a reply arrived but carries no interpretable intent. Recorded above;
        # nothing transitions (still not a timeout or escalation -- R9).
        return case

    excerpt = _reply_excerpt(reply)

    if intent is ReplyIntent.ADDITIONAL_INFO_REQUEST:
        return await flag_requires_human_action(
            session,
            case,
            notifier,
            kind="additional_info_requested",
            summary=(
                f"{case.site} ({case.domain}) replied asking for additional information or "
                f"verification before it will process the deletion request. Case {case.id} needs "
                f"your attention. Reason: {classification.reasoning}\n\n{excerpt}"
            ),
            detail={
                "message_id": reply.message_id,
                "reasoning": classification.reasoning,
                # Read back by `agent_loop._thread_reference_ids()` so the eventual resend threads
                # as an actual reply (`In-Reply-To`/`References`) to this exact message, not a
                # fresh, unrelated email -- see that function's docstring.
                "reply_message_id_header": reply.rfc822_message_id,
            },
        )

    if intent is ReplyIntent.CONFIRMATION:
        # Auto-close: no separate human confirm click for this intent (maintainer-directed
        # narrowing of issue #68 -- see this module's docstring, "Why CONFIRMATION auto-closes
        # now"). Records the exact same kind of `followup_confirmed` Event `case_actions.
        # confirm_followup()` records for a human-confirmed REJECTION, just agent-authored, then
        # applies it through the one guarded transition function both paths share.
        confirmation_event = create_event(
            case_id=case.id,
            kind="followup_confirmed",
            actor="agent",
            detail={
                "action": "confirm_deletion",
                "target_state": CaseState.DELETION_CONFIRMED.value,
            },
        )
        session.add(confirmation_event)
        session.flush()  # must be persisted (.id is not None) before the guard below accepts it
        apply_confirmed_followup_transition(
            session, case, confirmation_event, CaseState.DELETION_CONFIRMED
        )
        await notifier.notify(
            NotificationMessage(
                case_id=str(case.id),
                text=(
                    f"{case.site} ({case.domain}) appears to confirm the deletion is complete. "
                    f"Case {case.id} has been closed as '{CaseState.DELETION_CONFIRMED.value}'. "
                    f"If this looks wrong, use the 'reopen' action (kanban/CLI) to move it back "
                    f"to '{CaseState.REQUIRES_HUMAN_ACTION.value}'. "
                    f"Agent's reasoning: {classification.reasoning}\n\n{excerpt}"
                ),
            )
        )
        return case

    if intent is ReplyIntent.REJECTION:
        await notifier.notify(
            NotificationMessage(
                case_id=str(case.id),
                text=(
                    f"{case.site} ({case.domain}) appears to refuse or be unable to process the "
                    f"deletion request. Case {case.id}: confirm or dispute via kanban/CLI to "
                    f"close it as '{CaseState.REJECTED_ESCALATED.value}'. "
                    f"Agent's reasoning: {classification.reasoning}\n\n{excerpt}"
                ),
            )
        )
        return case

    # Every `ReplyIntent` member is handled above; this is unreachable unless a new member is added
    # without a branch here. Fail loud rather than silently leaving the case unadvanced.
    raise RuntimeError(f"unhandled reply intent: {intent!r}")  # pragma: no cover


def selected_documents(session: Session, case_id: int) -> list[Document]:
    """The vault documents (issue #111) currently selected for `case_id`'s current round, if any --
    an empty list if none are selected (never `None`; callers just check truthiness).
    `case_actions.attach_document()`/`select_documents_for_resend()` are the only writers of a
    `documents_selected_for_resend` `Event`, which this reads back.

    Public (not `_`-prefixed): real report -- neither `compose_additional_info_draft()`
    (this module's `draft_additional_info_response()` below) nor `case_actions.approve_draft()`
    ever calls `send_request()` themselves; the actual send only ever happens through
    `cli.run_once()`'s generic "send every approved `Draft ready` case" loop, which has no way to
    know a given draft has vault documents selected for it unless it looks this up itself --
    `_execute_send()`'s `documents` parameter defaults to `()`, so without this, that loop would
    silently send an additional-info round's resend with no attachment at all, every time. `cli.py`
    calls this directly before its own `send_request()` call for exactly this reason.

    Real report: the most recent selection isn't automatically "the answer to the current
    request" -- a selection recorded for an *earlier* round that somehow never got consumed is
    still on record, and the case has since moved on to a *new* `additional_info_requested` round
    asking for something else entirely. Using that stale selection on the new round's resend
    silently answers the wrong question with irrelevant, previously-selected content. A selection
    only counts as pending for the *current* round if it was recorded after the most recent
    `additional_info_requested` `Event` -- i.e. genuinely in response to what the site is asking
    for right now, not left over from before (same freshness rule this function's single-document
    predecessor, `pending_document()`, established).
    """
    selections = sorted(
        session.exec(
            select(Event).where(
                Event.case_id == case_id, Event.kind == "documents_selected_for_resend"
            )
        ).all(),
        key=lambda event: event.id or 0,
    )
    if not selections:
        return []
    latest_selection = selections[-1]

    requested_events = session.exec(
        select(Event).where(Event.case_id == case_id, Event.kind == "additional_info_requested")
    ).all()
    if not requested_events:
        # Defensive: `selected_documents()` is only meaningful once a case has actually reached
        # `Requires human action`, which always records one of these first -- but if one is
        # somehow missing, there is no "current round" to check freshness against, so refuse to
        # guess rather than attach a document to an undefined request.
        return []
    latest_request = max(requested_events, key=lambda event: event.id or 0)

    if (latest_selection.id or 0) <= (latest_request.id or 0):
        return []  # stale: selected before the current round's request, not an answer to it

    document_ids = latest_selection.detail.get("document_ids")
    if not isinstance(document_ids, list):
        return []
    documents: list[Document] = []
    for document_id in document_ids:
        document = session.get(Document, document_id)
        # Silently dropped, not a hard failure, if a selected document was since deleted from the
        # vault -- defensive: the other selected documents (if any) are still a genuine, if
        # partial, answer to the current request.
        if document is not None and document.status is DocumentStatus.AVAILABLE:
            documents.append(document)
    return documents


def _pending_text_response(session: Session, case_id: int) -> str | None:
    """The most recent not-yet-consumed `additional_info_text_provided` `Event`'s text for
    `case_id`, or `None`. "Not yet consumed" mirrors `agent_loop.
    _existing_approval_granted_event()`'s staleness shape: if a `draft_proposed` event exists after
    it, a resend has already used it."""
    provided = sorted(
        session.exec(
            select(Event).where(
                Event.case_id == case_id, Event.kind == "additional_info_text_provided"
            )
        ).all(),
        key=lambda event: event.id or 0,
    )
    if not provided:
        return None
    latest = provided[-1]
    already_consumed = any(
        (event.id or 0) > (latest.id or 0)
        for event in session.exec(
            select(Event).where(Event.case_id == case_id, Event.kind == "draft_proposed")
        ).all()
    )
    if already_consumed:
        return None
    text = latest.detail.get("text")
    return text if isinstance(text, str) else None


async def draft_additional_info_response(
    session: Session, case: Case, llm: LLMProvider, *, model: str
) -> Case:
    """The explicit, synchronous "Draft" action (module docstring, "Why drafting is now an
    explicit, synchronous action") -- resolves the human's currently pending answer for `case`'s
    current `Requires human action` round and composes a preview draft from it via `agent_loop.
    compose_additional_info_draft()`. Replaces the old background poll,
    `check_for_pending_additional_info()`, now that drafting is a direct human action rather than
    something a loop had to guess had become ready.

    Requires `case.state == Requires human action` (`ValueError` otherwise, mirroring
    `compose_additional_info_draft()`'s own precondition) and at least one of a *fresh* text answer
    (`_pending_text_response()`) or *fresh* vault-document selection (`selected_documents()`)
    currently on record for the case's current round -- `ValueError` otherwise (nothing to draft
    from yet; callers translate this to their own "invalid input" shape, same as any other
    `agent_loop`/`compose_additional_info_draft()` precondition failure). Both may be pending at
    once -- a human may use both channels in the same round (the LLM-drafted-reply extension's
    whole point: text becomes the draft's body, documents are attached alongside it) -- both are
    resolved and passed through to `compose_additional_info_draft()` together rather than one
    winning over the other.

    `llm`/`model` are injected (never constructed here -- R16/R17) and forwarded to
    `compose_additional_info_draft()`, which only actually calls the LLM when a fresh text answer
    is present (a documents-only draft needs no reply body to compose).
    """
    if case.state != CaseState.REQUIRES_HUMAN_ACTION:
        raise ValueError(
            f"draft_additional_info_response requires case.state == "
            f"{CaseState.REQUIRES_HUMAN_ACTION!r}, got {case.state!r}"
        )
    if case.id is None:
        raise RuntimeError("case must be persisted (have an id) before it can be advanced")

    documents = selected_documents(session, case.id)
    text = _pending_text_response(session, case.id)
    if not documents and text is None:
        raise ValueError(
            f"case {case.id} has no fresh document selection or text response on record for the "
            "current round -- provide at least one before drafting"
        )

    return await compose_additional_info_draft(
        session, case, llm, model, extra_text=text, documents=documents
    )
