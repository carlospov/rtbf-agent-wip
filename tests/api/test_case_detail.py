"""Tests for the case detail route (issue #55): `rtbf_agent.api.case_detail` +
`templates/case_detail.html`.

Three concerns, mirrored from the issue's Verification section:

- `test_case_detail_shows_every_event_in_chronological_order`: every `Event` for a `Case` renders,
  oldest first (`Event.id` ascending -- see `case_detail.py`'s module docstring for why this is
  "chronological" here, mirroring `tests/test_agent_loop_mock.py`'s `_events_for()` helper).
- `test_no_literal_user_facing_strings_in_bot_or_kanban_code`: this issue's case-detail half of the
  shared test name `tests/api/test_kanban.py` established for the kanban board half (see that
  file's own module docstring for why the Telegram bot half, `tests/plugins/notifiers/
  test_telegram.py`'s `test_no_literal_user_facing_strings_in_bot_code`, dropped "or_kanban" from
  its own name during the automated-case-loop rework while this file and `test_kanban.py` keep it,
  both genuinely covering kanban-specific code). The two
  check functions below (`_route_literal_dict_value_violations`,
  `_template_literal_text_violations`) are a deliberate line-for-line copy of `test_kanban.py`'s --
  not re-derived from scratch -- specifically so this module doesn't reintroduce a bypass gap
  `test_kanban.py`'s checks already had to close (PR #58's code review found the attribute-value
  check missing `alt`/`title`/`placeholder`/`aria-label`/`aria-valuetext` initially; that fix is
  preserved here by copying the already-fixed version rather than re-writing a narrower one from
  memory). Not factored into a shared importable helper module: `tests/` has no top-level
  `__init__.py` (only `tests/api/__init__.py`/`tests/plugins/__init__.py` do), so pytest's default
  import mode resolves this file as a bare `test_case_detail` module, not `tests.api.
  test_case_detail` -- a cross-file `from tests.api._r23_checks import ...` would depend on
  incidental pytest import-mode behavior rather than a real package boundary. Duplication here is
  self-contained and each check still carries its own catches-a-real-violation self-test below,
  same as `test_kanban.py`.
- `test_document_event_details_never_render_raw_content`: proves `case_detail.py`'s
  `_document_detail_items()` helper -- the only place a document-kind `Event`'s `detail` is turned
  into rendered fields -- is a closed allowlist (hash/size/extension), not a generic
  `detail.items()` walk that would just happen to be safe because the database never has more.
  Feeds it a plain `dict` (not a real `Event`, so `core.models`' own insert-time validation is
  irrelevant here -- this test is about the *route's* code, not the model's) containing an extra,
  disallowed key holding what would be raw content, and asserts it never reaches the returned
  items. A second, end-to-end variant renders a real (validly-constructed) document event through
  the full route/template and asserts only the three allowed fields appear in the response body.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from rtbf_agent.api.app import app
from rtbf_agent.api.case_detail import _document_detail_items
from rtbf_agent.api.deps import get_session
from rtbf_agent.core.models import Case, CaseState, Document, DocumentStatus, create_event
from rtbf_agent.plugins.i18n.resource import ResourceI18nProvider

CASE_DETAIL_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "rtbf_agent" / "api" / "case_detail.py"
)
CASE_DETAIL_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "rtbf_agent"
    / "api"
    / "templates"
    / "case_detail.html"
)


@pytest.fixture
def session() -> Generator[Session, None, None]:
    # StaticPool + check_same_thread=False: TestClient runs sync routes in a worker thread, a
    # different thread than this fixture -- see tests/api/test_app.py's identical fixture.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def client(session: Session) -> Generator[TestClient, None, None]:
    def _override_get_session() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_session] = _override_get_session
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session, None)


def _make_case(session: Session) -> Case:
    case = Case(
        site="Example Site",
        domain="example.com",
        target_email="me@example.com",
        state=CaseState.DRAFT_READY,
    )
    session.add(case)
    session.commit()
    session.refresh(case)
    return case


# --------------------------------------------------------------------------------------------- #
# Chronological order.
# --------------------------------------------------------------------------------------------- #


def test_case_detail_shows_every_event_in_chronological_order(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)
    assert case.id is not None

    # Inserted in this exact order; kind values double as unique markers in the rendered body.
    kinds = ["classification_proposed", "draft_proposed", "approval_granted", "request_sent"]
    for kind in kinds:
        session.add(
            create_event(
                case_id=case.id,
                kind=kind,
                actor="agent" if kind != "approval_granted" else "human",
                detail={"note": f"detail for {kind}"},
            )
        )
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    body = response.text

    positions = [body.find(kind) for kind in kinds]
    assert all(pos != -1 for pos in positions), f"not every event kind rendered: {positions}"
    assert positions == sorted(positions), (
        "events must render in Event.id ascending (oldest-first) order, per case_detail.py's "
        "documented chronological-order decision"
    )


def test_case_detail_events_are_collapsed_by_default(client: TestClient, session: Session) -> None:
    """Each event's detail is behind a native `<details>` toggle, collapsed by default (no `open`
    attribute) -- a busy case's history shouldn't be a wall of fully-expanded text; a human expands
    only the phase(s) they want to inspect."""
    case = _make_case(session)
    assert case.id is not None
    session.add(
        create_event(
            case_id=case.id,
            kind="classification_proposed",
            actor="agent",
            detail={"summary": "a distinctive summary marker"},
        )
    )
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    body = response.text
    details_start = body.find("<details")
    assert details_start != -1
    # No `open` attribute on the tag itself -- collapsed by default.
    tag_end = body.find(">", details_start)
    assert "open" not in body[details_start:tag_end]
    # The summary line (kind/actor/timestamp) is inside a <summary>, always visible; the detail
    # content (e.g. the classification summary marker) is elsewhere inside the same <details>.
    summary_start = body.find("<summary", details_start)
    assert summary_start != -1
    assert "classification_proposed" in body[summary_start : body.find("</summary>", summary_start)]
    assert (
        "a distinctive summary marker"
        in body[details_start : body.find("</details>", details_start)]
    )


def test_case_detail_shows_empty_state_notice_when_no_events_exist(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)
    i18n = ResourceI18nProvider()
    empty_text = i18n.translate("case_detail.empty_events", "en")

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert empty_text in response.text


def test_case_detail_renders_case_summary_fields(client: TestClient, session: Session) -> None:
    case = _make_case(session)

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert "Example Site" in response.text
    assert "example.com" in response.text


def test_case_detail_shows_recipient_not_set_placeholder_when_unconfirmed(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert "Not yet confirmed" in response.text
    assert f'action="/cases/{case.id}/recipient"' in response.text


def test_case_detail_shows_confirmed_recipient_address(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)
    case.recipient_email = "privacy@example.com"
    session.add(case)
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert "privacy@example.com" in response.text
    assert "Not yet confirmed" not in response.text


def test_case_detail_shows_unverified_suggestion_from_latest_recipient_proposed_event(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)
    assert case.id is not None
    session.add(
        create_event(
            case_id=case.id,
            kind="recipient_proposed",
            actor="agent",
            detail={
                "recipient": "contact@example.com",
                "reasoning": "General contact address.",
                "sources": ["https://example.com/contact"],
            },
        )
    )
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert "contact@example.com" in response.text
    assert "General contact address." in response.text
    assert "https://example.com/contact" in response.text
    # Pre-filled into the confirm form's input value, so submitting unedited accepts it.
    assert 'value="contact@example.com"' in response.text


def test_case_detail_hides_recipient_confirm_form_once_the_case_leaves_draft_ready(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)
    case.state = CaseState.AWAITING_RESPONSE
    case.recipient_email = "privacy@example.com"
    session.add(case)
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert f'action="/cases/{case.id}/recipient"' not in response.text


def test_case_detail_hides_recipient_confirm_form_once_already_approved(
    client: TestClient, session: Session
) -> None:
    """Mirrors api/recipient.py's own R1 gate: once an approval_granted Event exists, the confirm
    form must not be offered even though the case is still Draft ready (send_request() hasn't run
    yet) -- submitting it would just 409."""
    case = _make_case(session)
    case.recipient_email = "approved@example.com"
    session.add(case)
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

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert f'action="/cases/{case.id}/recipient"' not in response.text
    assert "approved@example.com" in response.text  # still shown as the confirmed recipient


def test_case_detail_approve_button_requires_a_confirmed_recipient(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)  # DRAFT_READY, no recipient_email

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert f'action="/cases/{case.id}/approve"' not in response.text

    case.recipient_email = "privacy@example.com"
    session.add(case)
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert f'action="/cases/{case.id}/approve"' in response.text


def test_case_detail_returns_404_for_a_missing_case(client: TestClient) -> None:
    response = client.get("/cases/999999")

    assert response.status_code == 404


# --------------------------------------------------------------------------------------------- #
# Jurisdiction/drafting-language review-and-override (no-prompt-at-creation reversal): only
# editable while the Case is still Discovered, mirroring case_actions.set_case_jurisdiction()/
# set_case_drafting_language()'s own gate.
# --------------------------------------------------------------------------------------------- #


def _make_discovered_case(session: Session) -> Case:
    case = Case(
        site="Example Site",
        domain="example.com",
        target_email="me@example.com",
        state=CaseState.DISCOVERED,
    )
    session.add(case)
    session.commit()
    session.refresh(case)
    return case


def test_case_detail_shows_case_override_forms_while_discovered(
    client: TestClient, session: Session
) -> None:
    case = _make_discovered_case(session)

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert f'action="/cases/{case.id}/jurisdiction"' in response.text
    assert f'action="/cases/{case.id}/drafting-language"' in response.text


def test_case_detail_hides_case_override_forms_once_past_discovered(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)  # DRAFT_READY

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert f'action="/cases/{case.id}/jurisdiction"' not in response.text
    assert f'action="/cases/{case.id}/drafting-language"' not in response.text


def test_case_detail_prefills_case_override_forms_with_current_values(
    client: TestClient, session: Session
) -> None:
    case = _make_discovered_case(session)
    case.applicable_jurisdiction = "GDPR"
    case.drafting_language = "fr"
    session.add(case)
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert 'value="GDPR"' in response.text
    assert 'value="fr"' in response.text


def test_case_detail_summary_shows_jurisdiction_and_drafting_language_regardless_of_state(
    client: TestClient, session: Session
) -> None:
    """The case-summary block always shows both fields' current value (or blank), even once the
    case is past Discovered and the edit forms are gone."""
    case = _make_case(session)  # DRAFT_READY
    case.applicable_jurisdiction = "GDPR"
    case.drafting_language = "fr"
    session.add(case)
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert "GDPR" in response.text
    assert "fr" in response.text


def test_set_jurisdiction_route_happy_path_redirects_to_case_detail(
    client: TestClient, session: Session
) -> None:
    case = _make_discovered_case(session)

    response = client.post(
        f"/cases/{case.id}/jurisdiction", data={"jurisdiction": "GDPR"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/cases/{case.id}"
    session.refresh(case)
    assert case.applicable_jurisdiction == "GDPR"


def test_set_jurisdiction_route_404_for_unknown_case(client: TestClient) -> None:
    response = client.post("/cases/999999/jurisdiction", data={"jurisdiction": "GDPR"})
    assert response.status_code == 404


def test_set_jurisdiction_route_409_once_past_discovered(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)  # DRAFT_READY

    response = client.post(f"/cases/{case.id}/jurisdiction", data={"jurisdiction": "GDPR"})

    assert response.status_code == 409


def test_set_jurisdiction_route_400_for_blank_value(client: TestClient, session: Session) -> None:
    case = _make_discovered_case(session)

    response = client.post(f"/cases/{case.id}/jurisdiction", data={"jurisdiction": "   "})

    assert response.status_code == 400


def test_set_drafting_language_route_happy_path_redirects_to_case_detail(
    client: TestClient, session: Session
) -> None:
    case = _make_discovered_case(session)

    response = client.post(
        f"/cases/{case.id}/drafting-language",
        data={"drafting_language": "fr"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/cases/{case.id}"
    session.refresh(case)
    assert case.drafting_language == "fr"


def test_set_drafting_language_route_404_for_unknown_case(client: TestClient) -> None:
    response = client.post("/cases/999999/drafting-language", data={"drafting_language": "fr"})
    assert response.status_code == 404


def test_set_drafting_language_route_409_once_past_discovered(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)  # DRAFT_READY

    response = client.post(f"/cases/{case.id}/drafting-language", data={"drafting_language": "fr"})

    assert response.status_code == 409


def test_set_drafting_language_route_400_for_blank_value(
    client: TestClient, session: Session
) -> None:
    case = _make_discovered_case(session)

    response = client.post(f"/cases/{case.id}/drafting-language", data={"drafting_language": " "})

    assert response.status_code == 400


# --------------------------------------------------------------------------------------------- #
# Follow-up confirm/dispute + additional-info sections (automated-case-loop rework, Phase 5).
# --------------------------------------------------------------------------------------------- #


def test_case_detail_shows_followup_confirm_dispute_when_a_classification_is_pending(
    client: TestClient, session: Session
) -> None:
    """Only a REJECTION is ever pending now (maintainer-directed narrowing of issue #68:
    `follow_up()` resolves a CONFIRMATION classification itself -- see `followup.py`'s module
    docstring, "Why CONFIRMATION auto-closes now")."""
    case = _make_case(session)
    case.state = CaseState.AWAITING_RESPONSE
    session.add(case)
    assert case.id is not None
    session.add(create_event(case_id=case.id, kind="request_sent", actor="agent"))
    session.add(
        create_event(
            case_id=case.id,
            kind="response_classified",
            actor="agent",
            detail={"message_id": "reply-1", "intent": "rejection", "reasoning": "Refused."},
        )
    )
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert f'action="/cases/{case.id}/followup/confirm"' in response.text
    assert f'action="/cases/{case.id}/followup/dispute"' in response.text
    assert "Refused." in response.text


def test_case_detail_hides_followup_section_once_confirmed(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)
    case.state = CaseState.DELETION_CONFIRMED
    session.add(case)
    assert case.id is not None
    session.add(create_event(case_id=case.id, kind="request_sent", actor="agent"))
    session.add(
        create_event(
            case_id=case.id,
            kind="response_classified",
            actor="agent",
            detail={"message_id": "reply-1", "intent": "confirmation", "reasoning": "Deleted."},
        )
    )
    session.add(
        create_event(
            case_id=case.id,
            kind="followup_confirmed",
            actor="human",
            detail={"action": "confirm_deletion", "target_state": "Deletion confirmed"},
        )
    )
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert f'action="/cases/{case.id}/followup/confirm"' not in response.text
    assert f'action="/cases/{case.id}/followup/dispute"' not in response.text


def test_case_detail_shows_reopen_button_when_deletion_confirmed(
    client: TestClient, session: Session
) -> None:
    """`can_reopen` -- the mitigating control for `follow_up()`'s CONFIRMATION auto-close: only a
    `Deletion confirmed` case offers the "reopen" button."""
    case = _make_case(session)
    case.state = CaseState.DELETION_CONFIRMED
    session.add(case)
    session.commit()
    assert case.id is not None

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert f'action="/cases/{case.id}/followup/reopen"' in response.text


def test_case_detail_hides_reopen_button_when_not_deletion_confirmed(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)
    case.state = CaseState.AWAITING_RESPONSE
    session.add(case)
    session.commit()
    assert case.id is not None

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert f'action="/cases/{case.id}/followup/reopen"' not in response.text


def test_case_detail_shows_additional_info_forms_while_requires_human_action(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)
    case.state = CaseState.REQUIRES_HUMAN_ACTION
    session.add(case)
    assert case.id is not None
    session.add(
        create_event(
            case_id=case.id,
            kind="additional_info_requested",
            actor="agent",
            detail={"reasoning": "site asked for date of birth"},
        )
    )
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert f'action="/cases/{case.id}/text-response"' in response.text
    assert f'action="/cases/{case.id}/document"' in response.text
    # Drafting-is-a-preview redesign (superseding issue #117/#119's fused "this round is ready"
    # draft+send button): a third, deliberately separate "Draft" form/button alongside the two
    # recording forms above -- typing text or attaching a document never itself drafts or sends.
    assert f'action="/cases/{case.id}/additional-info/draft"' in response.text
    assert "site asked for date of birth" in response.text


def test_case_detail_shows_pending_additional_info_text_and_prefills_textarea(
    client: TestClient, session: Session
) -> None:
    """Maintainer live-UX feedback: a text answer already submitted for the current round must be
    visible on the page (not just implicitly consumed by a subsequent draft), since the textarea
    itself always renders empty on a fresh page load regardless of what was previously typed."""
    case = _make_case(session)
    case.state = CaseState.REQUIRES_HUMAN_ACTION
    session.add(case)
    assert case.id is not None
    session.add(
        create_event(
            case_id=case.id,
            kind="additional_info_requested",
            actor="agent",
            detail={"reasoning": "site asked for date of birth"},
        )
    )
    session.add(
        create_event(
            case_id=case.id,
            kind="additional_info_text_provided",
            actor="human",
            detail={"text": "here is my date of birth: 1990-01-01"},
        )
    )
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    # Shown three times in total: the pending-note callout, the pre-filled textarea, and the
    # (collapsed) event-history entry -- assert the first two explicitly rather than just a count,
    # so this doesn't silently pass if either is dropped while the event-history copy remains.
    assert (
        '<div class="pending-text-note"><strong>Your current note for this round:</strong> '
        "here is my date of birth: 1990-01-01</div>" in response.text
    )
    assert (
        '<textarea id="text" name="text" rows="3" maxlength="10000" required>'
        "here is my date of birth: 1990-01-01</textarea>" in response.text
    )


def test_case_detail_hides_additional_info_forms_outside_requires_human_action(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)  # Draft ready

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert f'action="/cases/{case.id}/text-response"' not in response.text
    assert f'action="/cases/{case.id}/document"' not in response.text
    assert f'action="/cases/{case.id}/additional-info/draft"' not in response.text


def test_case_detail_shows_edit_draft_section_while_draft_ready_and_not_yet_approved(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)  # Draft ready, no approval recorded
    assert case.id is not None
    session.add(
        create_event(
            case_id=case.id,
            kind="draft_proposed",
            actor="agent",
            detail={"draft": "Please delete my data.", "subject": "Deletion request"},
        )
    )
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert f'action="/cases/{case.id}/edit-draft"' in response.text
    assert "Please delete my data." in response.text  # the textarea is pre-filled
    # The vault section is now also offered here, not just Requires human action (empty here since
    # no vault document exists yet -- see the "select-documents form" test below for a non-empty
    # vault rendering the actual selection form).
    assert "Select from vault" in response.text


def test_case_detail_hides_edit_draft_section_once_approved(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)
    assert case.id is not None
    session.add(
        create_event(
            case_id=case.id,
            kind="draft_proposed",
            actor="agent",
            detail={"draft": "Please delete my data.", "subject": "Deletion request"},
        )
    )
    session.add(
        create_event(
            case_id=case.id,
            kind="approval_granted",
            actor="human",
            detail={"action": "send_request"},
        )
    )
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert f'action="/cases/{case.id}/edit-draft"' not in response.text


def test_case_detail_hides_edit_draft_section_outside_draft_ready(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)
    case.state = CaseState.REQUIRES_HUMAN_ACTION
    session.add(case)
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert f'action="/cases/{case.id}/edit-draft"' not in response.text


def test_case_detail_shows_edit_draft_and_approve_reject_for_a_pending_additional_info_draft(
    client: TestClient, session: Session
) -> None:
    """The drafting-is-a-preview redesign's actual maintainer-facing point: a case still nominally
    `Requires human action` with a preview draft composed this round (`followup.
    draft_additional_info_response()`) renders the exact same edit/approve/reject UI a `Draft
    ready` case gets -- without `case.state` ever appearing to have moved. Regression-tests both
    halves at once: the widened `can_edit_draft`/`can_approve` render the sections, and the
    case-summary block still honestly shows 'Requires human action', not 'Draft ready'."""
    case = _make_case(session)
    case.state = CaseState.REQUIRES_HUMAN_ACTION
    case.recipient_email = "privacy@example.com"  # can_approve also requires this be set
    session.add(case)
    assert case.id is not None
    session.add(
        create_event(case_id=case.id, kind="additional_info_requested", actor="agent", detail={})
    )
    session.add(
        create_event(
            case_id=case.id,
            kind="draft_proposed",
            actor="agent",
            detail={"draft": "A previewed reply.", "subject": "Deletion request"},
        )
    )
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert f'action="/cases/{case.id}/edit-draft"' in response.text
    assert "A previewed reply." in response.text  # the textarea is pre-filled
    assert f'action="/cases/{case.id}/approve"' in response.text
    assert f'action="/cases/{case.id}/reject"' in response.text
    # The actual maintainer-facing assertion: the case still shows as Requires human action, not
    # Draft ready -- drafting never moved case.state.
    assert "Requires human action" in response.text
    assert "Draft ready" not in response.text


def test_case_detail_hides_edit_draft_for_requires_human_action_with_no_pending_draft(
    client: TestClient, session: Session
) -> None:
    """Regression guard for the widened gate: merely being `Requires human action` (with an
    `additional_info_requested` event but no drafted preview yet) must not render the edit/approve/
    reject section -- there is nothing to review yet."""
    case = _make_case(session)
    case.state = CaseState.REQUIRES_HUMAN_ACTION
    session.add(case)
    assert case.id is not None
    session.add(
        create_event(case_id=case.id, kind="additional_info_requested", actor="agent", detail={})
    )
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert f'action="/cases/{case.id}/edit-draft"' not in response.text
    assert f'action="/cases/{case.id}/approve"' not in response.text


def test_case_detail_hides_edit_draft_for_a_draft_already_consumed_by_an_earlier_approval(
    client: TestClient, session: Session
) -> None:
    """Code-reviewer finding on this PR: a case that completed a full additional-info round
    (drafted, approved, sent) and later returns to `Requires human action` via
    `dispute_followup()`/`reopen_case()` (neither records a fresh `additional_info_requested`
    event) must not render the edit/approve/reject section for that old, already-sent draft --
    it isn't a fresh, unreviewed preview just because no newer `additional_info_requested` event
    exists to compare it against."""
    case = _make_case(session)
    case.state = CaseState.REQUIRES_HUMAN_ACTION
    session.add(case)
    assert case.id is not None
    session.add(
        create_event(case_id=case.id, kind="additional_info_requested", actor="agent", detail={})
    )
    session.add(
        create_event(
            case_id=case.id,
            kind="draft_proposed",
            actor="agent",
            detail={"draft": "already sent", "subject": "Deletion request"},
        )
    )
    session.add(
        create_event(
            case_id=case.id,
            kind="approval_granted",
            actor="human",
            detail={"action": "send_request"},
        )
    )
    session.add(create_event(case_id=case.id, kind="request_sent", actor="agent", detail={}))
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    assert f'action="/cases/{case.id}/edit-draft"' not in response.text
    assert f'action="/cases/{case.id}/approve"' not in response.text


def test_case_detail_does_not_crash_when_draft_ready_has_no_draft_proposed_event_yet(
    client: TestClient, session: Session
) -> None:
    """Defensive: a case somehow reaching Draft ready with no draft_proposed event on record (e.g.
    a test fixture, or a future bug) must not make this route 500 -- the edit-draft section simply
    renders with no pre-filled body rather than the route raising."""
    case = _make_case(session)  # Draft ready, no draft_proposed event added

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200


def test_case_detail_marks_a_currently_selected_vault_document_as_checked(
    client: TestClient, session: Session
) -> None:
    """Maintainer live-UX feedback (PR #115 follow-up): re-opening this page after selecting a
    vault document for the round must show that it's actually selected -- a checkbox that always
    renders unchecked regardless of prior selection leaves a human with no way to tell "did my
    earlier click take" before submitting again or approving. `currently_selected_document_ids`
    reuses `followup.selected_documents()`, the exact same freshness-checked read
    `cli.run_once()`'s send loop already uses to decide what gets attached -- constructed here via
    the same requested-then-selected event ordering that function's own freshness check requires.
    """
    case = _make_case(session)  # Draft ready
    assert case.id is not None
    session.add(
        create_event(
            case_id=case.id,
            kind="draft_proposed",
            actor="agent",
            detail={"draft": "Please delete my data.", "subject": "Deletion request"},
        )
    )
    session.commit()

    selected_doc = Document(
        case_id=case.id,
        encrypted_path="/nonexistent/selected",
        mime_type="image/png",
        size=123,
        sha256_hash="selected-hash",
        label="ID card",
        status=DocumentStatus.AVAILABLE,
    )
    unselected_doc = Document(
        case_id=case.id,
        encrypted_path="/nonexistent/unselected",
        mime_type="image/png",
        size=456,
        sha256_hash="unselected-hash",
        label="Proof of address",
        status=DocumentStatus.AVAILABLE,
    )
    session.add(selected_doc)
    session.add(unselected_doc)
    session.commit()
    session.refresh(selected_doc)
    session.refresh(unselected_doc)

    # selected_documents()'s freshness check requires the selection to come after the round's own
    # additional_info_requested event -- both events added here, in that order, for exactly that
    # reason (see followup.selected_documents()'s own docstring).
    session.add(
        create_event(
            case_id=case.id,
            kind="additional_info_requested",
            actor="agent",
            detail={"reasoning": "site asked for a photo ID"},
        )
    )
    session.commit()
    session.add(
        create_event(
            case_id=case.id,
            kind="documents_selected_for_resend",
            actor="human",
            detail={"document_ids": [selected_doc.id]},
        )
    )
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    body = response.text
    selected_input = (
        f'<input type="checkbox" name="document_ids" value="{selected_doc.id}" checked>'
    )
    unselected_input = f'<input type="checkbox" name="document_ids" value="{unselected_doc.id}">'
    assert selected_input in body
    assert unselected_input in body
    # The "currently attached" badge appears once, next to the selected document only.
    assert body.count("currently attached") == 1


def test_cases_count_route_is_not_swallowed_by_the_case_detail_route(
    client: TestClient, session: Session
) -> None:
    """`/cases/count` (api/app.py) must still resolve to the literal count route, not be parsed as
    `case_id="count"` by this route's `{case_id:int}` path -- see case_detail.py's module
    docstring for why the `:int` convertor specifically guarantees this regardless of router
    registration order."""
    response = client.get("/cases/count")

    assert response.status_code == 200
    assert response.json() == {"count": 0}


# --------------------------------------------------------------------------------------------- #
# Document event detail restriction (R5/R6).
# --------------------------------------------------------------------------------------------- #


def test_document_event_details_never_render_raw_content(
    client: TestClient, session: Session
) -> None:
    # Unit-level: `_document_detail_items` is the only function that turns a document-kind
    # Event's `detail` into rendered fields. Feed it a plain dict (bypassing core.models'
    # insert-time validation entirely, since this is testing the route's own code, not the
    # model's) carrying an extra, disallowed key holding what would be raw content.
    items = _document_detail_items(
        {
            "hash": "abc123",
            "size": 42,
            "extension": "pdf",
            "raw_content": "this is the actual document body, must never render",
        },
        "en",
    )
    rendered_values = {item["value"] for item in items}
    assert "this is the actual document body, must never render" not in rendered_values
    assert len(items) == 3, f"expected exactly hash/size/extension, got: {items}"

    # End-to-end: a validly-constructed document event (only hash/size/extension, as
    # create_event()/Event's own model-level validation requires) renders exactly those three
    # fields on the real page, via the real route + template.
    case = _make_case(session)
    assert case.id is not None
    session.add(
        create_event(
            case_id=case.id,
            kind="document_received",
            actor="human",
            detail={"hash": "deadbeef", "size": 1234, "extension": "png"},
        )
    )
    session.commit()

    response = client.get(f"/cases/{case.id}")

    assert response.status_code == 200
    body = response.text
    assert "deadbeef" in body
    assert "1234" in body
    assert "png" in body


def test_document_detail_items_omits_missing_optional_extension() -> None:
    items = _document_detail_items({"hash": "abc123", "size": 10}, "en")

    labels = [item["label"] for item in items]
    assert len(items) == 2
    assert not any("xtension" in label.lower() for label in labels)


# --------------------------------------------------------------------------------------------- #
# No-literal-user-facing-strings (R23), case-detail half.
#
# Deliberate copy of tests/api/test_kanban.py's checks -- see this module's docstring for why this
# isn't factored into a shared importable helper instead.
# --------------------------------------------------------------------------------------------- #


def _is_literal_str(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _route_literal_dict_value_violations(module_path: Path) -> list[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    violations: list[str] = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Dict):
                for value in node.values:
                    literal = _is_literal_str(value) if value is not None else None
                    if literal is not None:
                        violations.append(
                            f"{module_path}:{value.lineno}: literal string used as a dict "
                            f"value: {literal!r}"
                        )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "dict"
            ):
                for kw in node.keywords:
                    literal = _is_literal_str(kw.value)
                    if literal is not None:
                        violations.append(
                            f"{module_path}:{kw.value.lineno}: literal string used as a "
                            f"dict(...) keyword value: {literal!r}"
                        )
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Subscript):
                literal = _is_literal_str(node.value)
                if literal is not None:
                    violations.append(
                        f"{module_path}:{node.value.lineno}: literal string assigned via "
                        f"subscript: {literal!r}"
                    )
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Subscript)
                and node.value is not None
            ):
                literal = _is_literal_str(node.value)
                if literal is not None:
                    violations.append(
                        f"{module_path}:{node.value.lineno}: literal string assigned via "
                        f"annotated subscript: {literal!r}"
                    )
            elif isinstance(node, ast.DictComp):
                literal = _is_literal_str(node.value)
                if literal is not None:
                    violations.append(
                        f"{module_path}:{node.value.lineno}: literal string used as a dict "
                        f"comprehension value: {literal!r}"
                    )
    return violations


_TEXTUAL_ATTRS = ("alt", "title", "placeholder", "aria-label", "aria-valuetext")


def _strip_jinja(text: str) -> str:
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)
    text = re.sub(r"\{\{.*?\}\}", "", text, flags=re.DOTALL)
    text = re.sub(r"\{%.*?%\}", "", text, flags=re.DOTALL)
    return text


def _template_literal_text_violations(template_path: Path) -> list[str]:
    raw = template_path.read_text(encoding="utf-8")
    violations: list[str] = []

    for attr in _TEXTUAL_ATTRS:
        for match in re.finditer(rf'\b{re.escape(attr)}\s*=\s*"([^"]*)"', raw):
            value = _strip_jinja(match.group(1)).strip()
            if value:
                violations.append(
                    f"{template_path}: literal text found in {attr!r} attribute: {value!r}"
                )
        for match in re.finditer(rf"\b{re.escape(attr)}\s*=\s*'([^']*)'", raw):
            value = _strip_jinja(match.group(1)).strip()
            if value:
                violations.append(
                    f"{template_path}: literal text found in {attr!r} attribute: {value!r}"
                )

    stripped = re.sub(r"<script\b.*?</script>", "", raw, flags=re.IGNORECASE | re.DOTALL)
    stripped = re.sub(r"<style\b.*?</style>", "", stripped, flags=re.IGNORECASE | re.DOTALL)
    stripped = _strip_jinja(stripped)
    stripped = re.sub(r"<[^>]*>", "", stripped, flags=re.DOTALL)

    leftover = stripped.strip()
    if leftover:
        violations.append(
            f"{template_path}: literal text found outside Jinja/HTML constructs: {leftover!r}"
        )
    return violations


def test_no_literal_user_facing_strings_in_bot_or_kanban_code() -> None:
    violations = _route_literal_dict_value_violations(
        CASE_DETAIL_MODULE_PATH
    ) + _template_literal_text_violations(CASE_DETAIL_TEMPLATE_PATH)
    assert not violations, "\n".join(violations)


def test_route_literal_dict_value_check_catches_a_real_violation(tmp_path: Path) -> None:
    bad_module = tmp_path / "bad_case_detail.py"
    bad_module.write_text(
        "def case_detail():\n"
        '    context = {"page_title": "Case detail", "empty_events_text": "No events"}\n'
        "    return context\n"
    )

    violations = _route_literal_dict_value_violations(bad_module)

    assert violations
    assert any("Case detail" in v for v in violations)
    assert any("No events" in v for v in violations)


def test_route_literal_dict_value_check_catches_dict_call_syntax(tmp_path: Path) -> None:
    bad_module = tmp_path / "bad_case_detail.py"
    bad_module.write_text(
        "def case_detail():\n"
        "    return dict(page_title='Case detail', empty_events_text='No events')\n"
    )

    violations = _route_literal_dict_value_violations(bad_module)

    assert violations
    assert any("Case detail" in v for v in violations)


def test_route_literal_dict_value_check_catches_subscript_assignment(tmp_path: Path) -> None:
    bad_module = tmp_path / "bad_case_detail.py"
    bad_module.write_text(
        "def case_detail():\n"
        "    context = {}\n"
        '    context["page_title"] = "Case detail"\n'
        "    return context\n"
    )

    violations = _route_literal_dict_value_violations(bad_module)

    assert violations
    assert any("Case detail" in v for v in violations)


def test_route_literal_dict_value_check_catches_annotated_subscript_assignment(
    tmp_path: Path,
) -> None:
    bad_module = tmp_path / "bad_case_detail.py"
    bad_module.write_text(
        "def case_detail():\n"
        "    context = {}\n"
        '    context["page_title"]: str = "Case detail"\n'
        "    return context\n"
    )

    violations = _route_literal_dict_value_violations(bad_module)

    assert violations
    assert any("Case detail" in v for v in violations)


def test_route_literal_dict_value_check_catches_dict_comprehension(tmp_path: Path) -> None:
    bad_module = tmp_path / "bad_case_detail.py"
    bad_module.write_text(
        "def case_detail():\n    return {k: 'Case detail' for k in ['page_title']}\n"
    )

    violations = _route_literal_dict_value_violations(bad_module)

    assert violations
    assert any("Case detail" in v for v in violations)


def test_template_literal_text_check_catches_a_real_violation(tmp_path: Path) -> None:
    bad_template = tmp_path / "bad_case_detail.html"
    bad_template.write_text(
        "<html><body><h1>Case detail</h1><p>{{ empty_events_text }}</p></body></html>"
    )

    violations = _template_literal_text_violations(bad_template)

    assert violations
    assert any("Case detail" in v for v in violations)


def test_template_literal_text_check_ignores_style_and_jinja_constructs(tmp_path: Path) -> None:
    ok_template = tmp_path / "ok_case_detail.html"
    ok_template.write_text(
        "<!DOCTYPE html>\n"
        "<html><head><style>body { color: red; }</style></head>\n"
        "<body>{# a comment #}{% for x in y %}<p>{{ x.label }}</p>{% endfor %}</body></html>\n"
    )

    violations = _template_literal_text_violations(ok_template)

    assert not violations


def test_template_literal_text_check_catches_hardcoded_attribute_values(tmp_path: Path) -> None:
    bad_template = tmp_path / "bad_case_detail.html"
    bad_template.write_text(
        '<html><body><img alt="Deletion confirmed">'
        '<div title="Approve this case">{{ x }}</div></body></html>'
    )

    violations = _template_literal_text_violations(bad_template)

    assert violations
    assert any("Deletion confirmed" in v for v in violations)
    assert any("Approve this case" in v for v in violations)


def test_template_literal_text_check_allows_jinja_inside_attribute_values(tmp_path: Path) -> None:
    ok_template = tmp_path / "ok_case_detail.html"
    ok_template.write_text('<html><body><img alt="{{ x.label }}"></body></html>')

    violations = _template_literal_text_violations(ok_template)

    assert not violations
