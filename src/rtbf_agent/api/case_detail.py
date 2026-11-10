"""Case detail route (issue #55): a `Case`'s full `Event` history in chronological order.

Renders `templates/case_detail.html` via the same `Jinja2Templates` instance construction pattern
as `rtbf_agent.api.kanban` -- see that module's docstring for the rationale behind server-rendered
HTML over an SPA, this file only repeats the mechanics needed to add a second route. This is also
the page the parent issue (#18) and this issue call out as the first place a human can review a
drafted request's actual body before approving it via Telegram -- the Telegram/console approval
prompt itself only ever shows a `case_id`/domain summary (see `agent_loop.send_request()`), never
the drafted text; this page closes that gap by rendering every `Event.detail`, including
`draft_proposed`'s `"draft"`/`"subject"` fields, in full.

**Route path:** `GET /cases/{case_id:int}` -- deliberately typed with Starlette's built-in `:int`
path convertor (not a bare `{case_id}`), so a request to the *literal* `/cases/count` route
(defined directly in `api/app.py`) can never be swallowed by this one. Starlette matches routes in
registration order using each path's compiled regex; `{case_id:int}` compiles to `\\d+`, which
`"count"` does not match, so Starlette falls through to the next candidate route regardless of
which router was `include_router()`-ed first. A bare `{case_id}` (implicit `str` convertor) would
instead match `"count"` literally and then fail Pydantic's int coercion with a 422, silently
breaking `/cases/count` depending on router registration order -- this route is written so that
class of registration-order bug cannot happen at all.

**404 behavior (explicit decision):** a `Case` id with no matching row raises `HTTPException(404)`
with an i18n-resolved `detail` string -- FastAPI's standard JSON error-response shape, exactly like
every other error path in this API (no other route in this app renders a custom HTML error page,
so this doesn't invent a second error-handling convention just for this one route).

**Chronological order:** `Event` rows for the case are sorted by `Event.id` ascending -- oldest
first -- mirroring `tests/test_agent_loop_mock.py`'s `_events_for()` helper, which is this repo's
existing precedent for "chronological" (`Event.id` is monotonically increasing on an insert-only,
never-updated table (R4), so ordering by it is equivalent to ordering by `timestamp` without
depending on wall-clock precision/uniqueness). Sorted in Python (`sorted(..., key=...)`), not via
`.order_by(Event.id)`: `Event.id` is typed `int | None` (nullable until a row is flushed), which
`ty` (this project's type checker) rejects as an `order_by()` argument via the class-level column
descriptor -- the same class of mismatch `kanban.py`'s module docstring documents for
`Case.updated_at`, and `agent_loop.py`'s `latest_draft()` already works around the same way.

**Document-event detail restriction (R5/R6), rendered deliberately, not just accidentally:** for
`Event.kind in DOCUMENT_EVENT_KINDS`, this route does **not** generically iterate
`event.detail.items()` the way it does for every other event kind. It instead reads only the fixed
allowlist of fields `core.models.validate_document_event_detail()` permits a document event's
`detail` to ever contain -- `hash`, `size`, an optional `extension` -- via
`_document_detail_items()` below, and nothing else. The database already cannot hold anything else
in a document event's `detail` (R5 is enforced at the model layer), so this distinction can never
be observed to matter against real data -- but the route's *code* still draws the line explicitly,
rather than being
"correct" purely as an artifact of the data never containing more. `test_document_event_details_
never_render_raw_content` (`tests/api/test_case_detail.py`) proves this at two levels: a unit test
feeds `_document_detail_items()` a plain `dict` (not a real `Event` -- `core.models`'s own
insert-time validation is irrelevant to testing this route's own code) carrying an extra,
disallowed key holding what would be raw content, and asserts it's excluded from the returned
items; an end-to-end variant renders a validly-constructed document event through the full route/
template and asserts only the allowed hash/size/extension fields appear in the response.

**Every other event kind's `detail`** is free-form, caller-defined data (a classification summary,
a drafted request body + subject, an approval/rejection action tag -- see `agent_loop.py`'s
`create_event(...)` call sites) -- rendered via `_generic_detail_items()`, which shows every
key/value pair in `detail`, sorted by key for a deterministic render order independent of dict
insertion order. Those keys and values are audit *data* already written to the database by the
agent/human, not UI copy this route authors -- so, unlike every literal label on this page, they
are not routed through `I18nProvider.translate()` (there is no lookup key for e.g. a drafted
request's own text -- it isn't a fixed, enumerable string this codebase controls the wording of).

**R23 -- every actual UI-authored string** (page title, field labels, empty-state notice, the
document-field labels) is sourced from `_i18n.translate(...)`, mirroring `kanban.py`'s invariant
and checked the same two ways by `tests/api/test_case_detail.py`: an AST walk over this module
flagging any literal string used as a rendering-context value, and a template-text scan (including
the `alt`/`title`/`placeholder`/`aria-label`/`aria-valuetext` attribute-value check) over
`templates/case_detail.html`.

**Interface language resolution:** `rtbf_agent.config.resolve_interface_language()`, the same
single-process-wide mechanism `kanban.py` and the Telegram bot use -- no second mechanism invented.

**Not in scope (per the issue):** no approve/reject action lives on this page.

**Follow-up confirm/dispute + additional-info sections (automated-case-loop rework, Phase 5):**
`pending_followup` is the latest unconsumed `response_classified` event with a `REJECTION` intent,
if any (read straight out of the `events` list already fetched below, the same "walk the already-
fetched chronological list" approach the recipient-suggestion block already uses) -- rendered with
Confirm/Dispute buttons posting to `api/followup.py`'s routes. Never a `CONFIRMATION` intent: a
later maintainer-directed narrowing of issue #68 (see `followup.py`'s module docstring, "Why
CONFIRMATION auto-closes now") made `follow_up()` resolve a `CONFIRMATION` classification
immediately, itself, so one is never left pending here for a human to confirm/dispute. A separate
`Requires human action` section (text/document forms + a "Draft" button posting to
`api/document.py`'s routes) is shown whenever `case.state == Requires human action`, rendering the
latest `additional_info_requested` event's reasoning so a human knows what's actually being asked
for.

**Drafting is a preview, not a transition (issue #117/#119, superseded -- see `followup.py`'s
module docstring, "Why drafting is now an explicit, synchronous action"):** clicking "Draft" calls
`api/document.py`'s `POST .../additional-info/draft` route, which composes a `draft_proposed`
`Event` without ever moving `case.state` off `Requires human action`. `has_pending_additional_info_
draft` (computed below from the same already-fetched `events` list, mirroring `case_actions.
_has_pending_additional_info_draft()`'s exact freshness shape -- a `draft_proposed` event recorded
after the round's `additional_info_requested` event) is what lets `can_edit_draft`/`can_approve`
below recognize that a case still nominally `Requires human action` already has a reviewable draft
on record, so the exact same edit/approve/reject UI a `Draft ready` case gets renders here too, with
the case-summary block above still honestly showing `Requires human action` as `case.state` the
whole time -- the maintainer-facing point of this redesign. Only actually clicking Approve
(`case_actions.approve_draft()`, see that function's docstring) ever flips `case.state` to `Draft
ready`, right before the existing recurring send-loop picks it up.

**Reopen (mitigating control for the `CONFIRMATION` auto-close above):** `can_reopen` is `True`
iff `case.state == CaseState.DELETION_CONFIRMED` -- rendered as a single "Reopen case" button
posting to `api/followup.py`'s new `POST /cases/{case_id}/followup/reopen` route
(`case_actions.reopen_case()`), moving the case back to `Requires human action` so a human can
undo a wrongly auto-closed case. Same UI-convenience-not-enforcement caveat as `can_approve`/
`pending_followup`: the route itself also refuses (409) for any other state.

**Recipient confirmation (follow-up to issue #94):** the case-summary block always shows the
current `Case.recipient_email` (or a "not yet confirmed" placeholder), and -- while the case is
`Draft ready` -- a form posting to `api.recipient.confirm_recipient` lets a human confirm or
correct it, pre-filled with the current value or, absent one, the latest `recipient_proposed`
event's suggested address (read straight out of the `events` list already fetched below, no extra
query). `can_approve` additionally requires `case.recipient_email is not None`: approving a send
with no recipient would only fail later at `agent_loop.send_request()`'s own guard, so there is no
reason to offer that button first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from rtbf_agent import case_actions
from rtbf_agent.api.deps import get_session
from rtbf_agent.api.kanban import CASE_STATE_LABEL_KEYS
from rtbf_agent.config import resolve_interface_language
from rtbf_agent.core.interfaces import I18nProvider
from rtbf_agent.core.models import DOCUMENT_EVENT_KINDS, Case, CaseState, Event
from rtbf_agent.followup import selected_documents
from rtbf_agent.plugins.i18n.resource import ResourceI18nProvider

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# One `I18nProvider` instance for the whole process -- same rationale as `kanban.py`'s `_i18n`
# (single self-hosted process, not a per-request concern).
_i18n: I18nProvider = ResourceI18nProvider()

# Fixed, ordered allowlist of the only fields ever rendered for a document-related Event's
# `detail` (R5/R6): hash, size, an optional extension -- never anything else, and never treated as
# if it were the document's actual content, because there is none in the database to render. Each
# field maps to its own i18n label key (order here is the render order, independent of whatever
# key order `detail` itself happens to have).
_DOCUMENT_DETAIL_FIELD_LABEL_KEYS: tuple[tuple[str, str], ...] = (
    ("hash", "case_detail.event.document_hash_label"),
    ("size", "case_detail.event.document_size_label"),
    ("extension", "case_detail.event.document_extension_label"),
)


def _document_detail_items(detail: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    """The only fields ever rendered for a document-kind `Event`'s `detail` -- see module docstring.

    Explicitly does not fall back to a generic `detail.items()` walk for any reason: even if a
    future bug somehow let an extra key reach this dict, this function would still never render
    it -- the allowlist in `_DOCUMENT_DETAIL_FIELD_LABEL_KEYS` is exhaustive and closed, not a
    "usually correct" filter.
    """
    items: list[dict[str, Any]] = []
    for key, label_key in _DOCUMENT_DETAIL_FIELD_LABEL_KEYS:
        if key not in detail:
            # `extension` is optional per `validate_document_event_detail()`; `hash`/`size` are
            # always present on any valid document event, but a missing one here is still handled
            # by omission rather than a KeyError -- this route only ever displays what's there.
            continue
        items.append({"label": _i18n.translate(label_key, lang), "value": detail[key]})
    return items


def _generic_detail_items(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """Every key/value pair in a non-document `Event`'s `detail`, sorted by key.

    Unlike `_document_detail_items()`, this has no fixed allowlist: `detail` for these kinds is
    free-form audit data the agent/human already wrote (a drafted request's own subject/body, a
    classification summary, an approval action tag) -- this is the page's whole point (reviewing a
    draft before approving its send), not something to filter or reshape.
    """
    return [{"label": key, "value": detail[key]} for key in sorted(detail)]


@router.get("/cases/{case_id:int}")
def case_detail(case_id: int, request: Request, session: Session = Depends(get_session)):
    """Render one `Case` plus every `Event` recorded for it, oldest first."""
    lang = resolve_interface_language()

    case = session.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=_i18n.translate("case_detail.not_found", lang))

    # Ascending by Event.id: oldest first -- see module docstring for why this is the
    # "chronological" ordering this repo already establishes elsewhere (_events_for() in
    # tests/test_agent_loop_mock.py). Sorted in Python, not via `.order_by(Event.id)`: `Event.id`
    # is typed `int | None` (nullable until a row is flushed), which `ty` (this project's type
    # checker) rejects as an `order_by()` argument via the class-level column descriptor -- the
    # same class of mismatch `kanban.py`'s module docstring documents for `Case.updated_at`, and
    # `agent_loop.py`'s `latest_draft()` already works around via `key=lambda e: e.id or 0`.
    # Every event here is already persisted (fetched back from the DB), so `.id` is never actually
    # `None` at runtime -- `or 0` only satisfies the static type, never changes the sort result.
    events = sorted(
        session.exec(select(Event).where(Event.case_id == case_id)).all(),
        key=lambda event: event.id or 0,
    )

    # Latest recipient_proposed event's suggestion, if any -- read from the same `events` list
    # already fetched above (chronological, oldest first), so the last match is the most recent.
    # Only used to pre-fill the confirm form; never written to `Case.recipient_email` here.
    recipient_suggestion: dict[str, Any] | None = None
    already_approved = False
    # The latest response_classified event not yet "consumed" by a later followup_confirmed/
    # followup_disputed event -- mirrors case_actions._latest_unconsumed_classification()'s
    # staleness shape, computed here from the same chronological `events` list rather than a
    # second query, since this route already walks it once for the recipient suggestion above.
    pending_classification: dict[str, Any] | None = None
    latest_additional_info_reasoning: str | None = None
    # `(latest approval_granted id, latest request_sent id)` -- tracked so the editable-draft
    # section below can compute the exact same "unconsumed approval" staleness check
    # `case_actions._has_unconsumed_approval_event()`/`edit_draft()` gate on (a case can cycle
    # through Draft ready more than once, so simply "has an approval_granted ever existed",
    # `already_approved` above, is not the right question here -- an earlier round's already-
    # consumed approval must not hide this round's edit section).
    latest_approval_granted_id: int | None = None
    latest_request_sent_id: int | None = None
    # The most recent draft_proposed event's own body -- read from the same already-fetched
    # `events` list (not a second `latest_draft()` query) so a case that somehow reaches Draft
    # ready with no draft_proposed event on record yet (e.g. a test fixture that sets `state`
    # directly) can't make this route 500 -- the editable-draft section below simply doesn't
    # pre-fill a body in that edge case, rather than `latest_draft()` raising.
    latest_draft_body: str | None = None
    # `(latest draft_proposed id, latest additional_info_requested id)` -- mirrors `case_actions.
    # _has_pending_additional_info_draft()`'s exact freshness shape (drafting-is-a-preview
    # redesign, see module docstring): a preview draft only counts as "pending" for the *current*
    # round if it was recorded after that round's own `additional_info_requested` event, not left
    # over from an earlier, already-resolved one.
    latest_draft_proposed_id: int | None = None
    latest_additional_info_requested_id: int | None = None
    # The most recent text answer a human has typed for the current round (maintainer live-UX
    # feedback, drafting-is-a-preview redesign): once submitted via `provide_text_response()`, the
    # textarea it came from resets empty on the next page load, so without surfacing this
    # separately there was no way to see what was actually on record before clicking "Draft".
    # Freshness mirrors `followup._pending_text_response()`'s rule (recorded after the round's own
    # `additional_info_requested` event) -- shown regardless of whether a draft has since been
    # composed from it, so re-opening the page after drafting still confirms what note the draft
    # (if any) was actually based on.
    latest_additional_info_text: str | None = None
    latest_additional_info_text_id: int | None = None
    for event in events:
        if event.kind == "recipient_proposed":
            recipient_suggestion = {
                "recipient": event.detail.get("recipient"),
                "reasoning": event.detail.get("reasoning"),
                "sources": event.detail.get("sources") or [],
            }
        elif event.kind == "approval_granted":
            already_approved = True
            latest_approval_granted_id = event.id
        elif event.kind == "request_sent":
            latest_request_sent_id = event.id
        elif event.kind == "draft_proposed":
            draft = event.detail.get("draft")
            latest_draft_body = draft if isinstance(draft, str) else None
            latest_draft_proposed_id = event.id
        elif event.kind == "response_classified":
            pending_classification = {
                "intent": event.detail.get("intent"),
                "reasoning": event.detail.get("reasoning"),
            }
        elif event.kind in ("followup_confirmed", "followup_disputed"):
            pending_classification = None
        elif event.kind == "additional_info_requested":
            reasoning = event.detail.get("reasoning")
            latest_additional_info_reasoning = reasoning if isinstance(reasoning, str) else None
            latest_additional_info_requested_id = event.id
        elif event.kind == "additional_info_text_provided":
            text = event.detail.get("text")
            latest_additional_info_text = text if isinstance(text, str) else None
            latest_additional_info_text_id = event.id

    pending_followup: dict[str, Any] | None = None
    if pending_classification is not None and pending_classification["intent"] == "rejection":
        pending_followup = pending_classification

    # Reopen (mitigating control for follow_up()'s CONFIRMATION auto-close -- see module docstring
    # and case_actions.reopen_case()): only a Deletion confirmed case has anything to undo.
    can_reopen = case.state == CaseState.DELETION_CONFIRMED

    # Jurisdiction/drafting-language review-and-override (no-prompt-at-creation reversal): only
    # editable while still Discovered, before agent_loop._classify() ever reads and locks these
    # fields in for the round -- mirrors case_actions.set_case_jurisdiction()/
    # set_case_drafting_language()'s own gate exactly (a UI convenience; the routes themselves also
    # refuse with 409 for any other state). Pre-filled below with the current (possibly None)
    # value -- None here is not "unset, will error," it's the honest "no per-case override yet,
    # deferring to the global default" state case_actions.py's docstring already documents.
    can_edit_case_overrides = case.state == CaseState.DISCOVERED

    has_unconsumed_approval = latest_approval_granted_id is not None and not (
        latest_request_sent_id is not None
        and (latest_approval_granted_id or 0) < (latest_request_sent_id or 0)
    )
    # Drafting-is-a-preview redesign (see module docstring): a case still nominally Requires human
    # action can already have a reviewable draft on record, exactly mirroring `case_actions.
    # _has_pending_additional_info_draft()` -- including that function's "not already consumed by
    # an approval_granted Event" half (code-reviewer finding on this PR): `dispute_followup()`/
    # `reopen_case()` can both return a case to Requires human action without a fresh
    # additional_info_requested Event at all, which would otherwise leave an earlier round's
    # already-approved-and-sent draft looking "fresh" against that same old request.
    has_pending_additional_info_draft = (
        latest_draft_proposed_id is not None
        and latest_additional_info_requested_id is not None
        and latest_draft_proposed_id > latest_additional_info_requested_id
        and not (
            latest_approval_granted_id is not None
            and latest_approval_granted_id > latest_draft_proposed_id
        )
    )
    # The editable-draft window (LLM-drafted-replies extension): either the original origin (a
    # draft exists -- Draft ready -- and hasn't been authorized to send yet, the exact same
    # condition `case_actions.edit_draft()`/the now-widened `select_documents_for_resend()`/
    # `attach_document()` gate on via `_editable_round_case()`'s `Draft ready` half), or the new
    # origin (still Requires human action, but a preview draft has been composed this round) --
    # mirrors `case_actions._send_eligible_case()`'s exact disjunction.
    can_edit_draft = (case.state == CaseState.DRAFT_READY and not has_unconsumed_approval) or (
        case.state == CaseState.REQUIRES_HUMAN_ACTION and has_pending_additional_info_draft
    )

    pending_additional_info_text = (
        latest_additional_info_text
        if latest_additional_info_text_id is not None
        and latest_additional_info_requested_id is not None
        and latest_additional_info_text_id > latest_additional_info_requested_id
        else None
    )

    can_provide_additional_info = case.state == CaseState.REQUIRES_HUMAN_ACTION
    # Vault documents can now also be selected/attached while reviewing an editable Draft ready
    # case (not just Requires human action) -- both conditions render the same vault-selection
    # form, posting to the same api/vault.py route, whose own gate already accepts both states.
    show_document_forms = can_provide_additional_info or can_edit_draft
    # Only queried when actually rendered (issue #111 OCR review finding): this page is hit on
    # every case view regardless of state, and `list_vault_documents()` is a real query -- no reason
    # to pay for it on every `Awaiting response`/`Deletion confirmed`/etc. case detail load.
    vault_documents = case_actions.list_vault_documents(session) if show_document_forms else []
    current_draft_body = latest_draft_body if can_edit_draft else None
    # Which of those vault documents are actually selected for *this* round right now (maintainer
    # live-UX feedback on this PR): `followup.selected_documents()` is the exact same freshness-
    # checked read `cli.run_once()`'s send loop already uses to decide what gets attached at send
    # time (see that function's own docstring -- only a selection recorded after the current
    # round's `additional_info_requested` event counts, anything from an earlier, already-answered
    # round is excluded) -- reusing it here, rather than a separate/looser query, means the
    # checkbox state a human sees can never claim something is "selected" that the send loop would
    # actually treat as stale and ignore, or vice versa.
    currently_selected_document_ids = (
        {document.id for document in selected_documents(session, case_id)}
        if show_document_forms
        else set()
    )

    rendered_events = []
    for event in events:
        if event.kind in DOCUMENT_EVENT_KINDS:
            detail_items = _document_detail_items(event.detail, lang)
        else:
            detail_items = _generic_detail_items(event.detail)
        rendered_events.append(
            {
                "kind": event.kind,
                "actor": event.actor,
                "timestamp": event.timestamp.isoformat(),
                "detail_items": detail_items,
            }
        )

    context = {
        "lang": lang,
        "page_title": _i18n.translate("case_detail.page_title", lang),
        "back_link_text": _i18n.translate("case_detail.back_link", lang),
        "case": {
            "id": case.id,
            "site": case.site,
            "domain": case.domain,
            "state": _i18n.translate(CASE_STATE_LABEL_KEYS[case.state], lang),
            "recipient_email": case.recipient_email,
            "applicable_jurisdiction": case.applicable_jurisdiction,
            "drafting_language": case.drafting_language,
        },
        "site_label": _i18n.translate("case_detail.site_label", lang),
        "domain_label": _i18n.translate("case_detail.domain_label", lang),
        "state_label": _i18n.translate("case_detail.state_label", lang),
        "events_heading": _i18n.translate("case_detail.events_heading", lang),
        "kind_label": _i18n.translate("case_detail.event.kind_label", lang),
        "actor_label": _i18n.translate("case_detail.event.actor_label", lang),
        "timestamp_label": _i18n.translate("case_detail.event.timestamp_label", lang),
        "empty_events_text": _i18n.translate("case_detail.empty_events", lang),
        "events": rendered_events,
        # Recipient confirmation (follow-up to #94): the summary line is always shown; the confirm
        # form only while there is still a recipient decision to make (Draft ready) AND no
        # approval_granted Event exists yet -- once approved, api/recipient.py's route itself
        # refuses further changes (R1: an approval decision must not be silently redirected to a
        # since-changed recipient before the CLI executes the send), so the form is hidden here too
        # rather than offering an action that would just 409.
        "recipient_label": _i18n.translate("case_detail.recipient_label", lang),
        "recipient_not_set_text": _i18n.translate("case_detail.recipient_not_set", lang),
        "can_confirm_recipient": case.state == CaseState.DRAFT_READY and not already_approved,
        "recipient_heading": _i18n.translate("case_detail.recipient_heading", lang),
        "recipient_suggestion": recipient_suggestion,
        "recipient_unverified_notice": _i18n.translate(
            "case_detail.recipient_unverified_notice", lang
        ),
        "recipient_reasoning_label": _i18n.translate("case_detail.recipient_reasoning_label", lang),
        "recipient_sources_label": _i18n.translate("case_detail.recipient_sources_label", lang),
        "recipient_input_label": _i18n.translate("case_detail.recipient_input_label", lang),
        "recipient_confirm_button_text": _i18n.translate(
            "case_detail.recipient_confirm_button", lang
        ),
        "recipient_confirm_hint": _i18n.translate("case_detail.recipient_confirm_hint", lang),
        # Jurisdiction/drafting-language review-and-override (no-prompt-at-creation reversal):
        # only rendered while still Discovered -- see can_edit_case_overrides' own comment above.
        # Two independent forms (R22), each pre-filled with the current, possibly-None value.
        "can_edit_case_overrides": can_edit_case_overrides,
        "case_overrides_heading": _i18n.translate("case_detail.case_overrides_heading", lang),
        "jurisdiction_label": _i18n.translate("case_detail.jurisdiction_label", lang),
        "jurisdiction_input_label": _i18n.translate("case_detail.jurisdiction_input_label", lang),
        "jurisdiction_set_button_text": _i18n.translate(
            "case_detail.jurisdiction_set_button", lang
        ),
        "jurisdiction_hint": _i18n.translate("case_detail.jurisdiction_hint", lang),
        "drafting_language_label": _i18n.translate("case_detail.drafting_language_label", lang),
        "drafting_language_input_label": _i18n.translate(
            "case_detail.drafting_language_input_label", lang
        ),
        "drafting_language_set_button_text": _i18n.translate(
            "case_detail.drafting_language_set_button", lang
        ),
        "drafting_language_hint": _i18n.translate("case_detail.drafting_language_hint", lang),
        # Approve/reject (issue #67): only rendered for a case actually eligible to send -- either
        # origin `can_edit_draft` above recognizes (Draft ready and not yet approved, or Requires
        # human action with a pending preview draft -- the drafting-is-a-preview redesign; mirrors
        # `case_actions._send_eligible_case()`'s exact disjunction). api/approval.py's routes
        # themselves also refuse (409) for any other state, so this is a UI convenience (don't show
        # a button that would fail), not the actual enforcement point. Also requires a confirmed
        # recipient -- approving with none set would only fail later at agent_loop.send_request()'s
        # own guard.
        "can_approve": can_edit_draft and case.recipient_email is not None,
        "approval_heading": _i18n.translate("case_detail.approval_heading", lang),
        "approve_button_text": _i18n.translate("case_detail.approve_button", lang),
        "reject_button_text": _i18n.translate("case_detail.reject_button", lang),
        # Follow-up confirm/dispute (automated-case-loop rework, Phase 5): only rendered while an
        # unconsumed CONFIRMATION/REJECTION classification is actually pending -- api/followup.py's
        # routes themselves also refuse (409) otherwise, this is a UI convenience.
        "pending_followup": pending_followup,
        "followup_heading": _i18n.translate("case_detail.followup_heading", lang),
        "followup_reasoning_label": _i18n.translate("case_detail.followup_reasoning_label", lang),
        "followup_confirm_button_text": _i18n.translate(
            "case_detail.followup_confirm_button", lang
        ),
        "followup_dispute_button_text": _i18n.translate(
            "case_detail.followup_dispute_button", lang
        ),
        "followup_confirm_hint": _i18n.translate("case_detail.followup_confirm_hint", lang),
        # Reopen (mitigating control for follow_up()'s CONFIRMATION auto-close): only rendered for
        # a Deletion confirmed case -- api/followup.py's reopen route itself also refuses (409)
        # otherwise, this is a UI convenience.
        "can_reopen": can_reopen,
        "reopen_heading": _i18n.translate("case_detail.reopen_heading", lang),
        "reopen_hint": _i18n.translate("case_detail.reopen_hint", lang),
        "reopen_button_text": _i18n.translate("case_detail.reopen_button", lang),
        # Requires human action / additional info (automated-case-loop rework, Phase 5): only
        # rendered while the case is actually in that state -- api/document.py's routes themselves
        # also refuse (409) otherwise.
        "can_provide_additional_info": can_provide_additional_info,
        "additional_info_summary": latest_additional_info_reasoning,
        "pending_additional_info_text": pending_additional_info_text,
        "additional_info_pending_text_label": _i18n.translate(
            "case_detail.additional_info_pending_text_label", lang
        ),
        "additional_info_heading": _i18n.translate("case_detail.additional_info_heading", lang),
        "additional_info_text_label": _i18n.translate(
            "case_detail.additional_info_text_label", lang
        ),
        "additional_info_text_button_text": _i18n.translate(
            "case_detail.additional_info_text_button", lang
        ),
        "additional_info_document_label": _i18n.translate(
            "case_detail.additional_info_document_label", lang
        ),
        "additional_info_document_button_text": _i18n.translate(
            "case_detail.additional_info_document_button", lang
        ),
        "additional_info_label_field": _i18n.translate(
            "case_detail.additional_info_label_field", lang
        ),
        "additional_info_hint": _i18n.translate("case_detail.additional_info_hint", lang),
        # "Draft" (drafting-is-a-preview redesign, superseding issue #117/#119's "this round is
        # ready" fused draft+send button -- see `followup.py`'s module docstring): a deliberate,
        # separate step from typing text or attaching a document, composing a preview only --
        # `case.state` never moves here. Rendered in the same can_provide_additional_info block as
        # the text/document forms above; api/document.py's route also refuses (409/400) otherwise.
        "additional_info_draft_hint": _i18n.translate(
            "case_detail.additional_info_draft_hint", lang
        ),
        "additional_info_draft_button_text": _i18n.translate(
            "case_detail.additional_info_draft_button", lang
        ),
        # Editable draft (LLM-drafted-replies extension): a draft body a human can hand-edit right
        # up until approval -- rendered whenever `can_edit_draft` (Draft ready and not yet approved,
        # or Requires human action with a pending preview draft), which applies uniformly to every
        # round, the very first draft included, not just resends.
        "can_edit_draft": can_edit_draft,
        "current_draft_body": current_draft_body,
        "edit_draft_heading": _i18n.translate("case_detail.edit_draft_heading", lang),
        "edit_draft_label": _i18n.translate("case_detail.edit_draft_label", lang),
        "edit_draft_button_text": _i18n.translate("case_detail.edit_draft_button", lang),
        "edit_draft_hint": _i18n.translate("case_detail.edit_draft_hint", lang),
        # Document vault (issue #111): shown alongside the upload form above (Requires human
        # action), or alongside the editable-draft section (Draft ready, not yet approved,
        # LLM-drafted-replies extension) -- lets a human select an existing vault document instead
        # of re-uploading it, including one originally uploaded on a different case (see
        # `case_actions.select_documents_for_resend()`'s own docstring for why that's allowed).
        "show_document_forms": show_document_forms,
        "vault_documents": [
            {
                "id": document.id,
                "label": document.label or f"document-{document.id}",
                "selected": document.id in currently_selected_document_ids,
            }
            for document in vault_documents
        ],
        "vault_selected_badge_text": _i18n.translate("case_detail.vault_selected_badge", lang),
        "vault_heading": _i18n.translate("case_detail.vault_heading", lang),
        "vault_hint": _i18n.translate("case_detail.vault_hint", lang),
        "vault_select_button_text": _i18n.translate("case_detail.vault_select_button", lang),
        "vault_empty_text": _i18n.translate("case_detail.vault_empty", lang),
        "vault_link_text": _i18n.translate("case_detail.vault_link_text", lang),
        "vault_download_link_text": _i18n.translate("case_detail.vault_download_link_text", lang),
    }
    return templates.TemplateResponse(request, "case_detail.html", context)
