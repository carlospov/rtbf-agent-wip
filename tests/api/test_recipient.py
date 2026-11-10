"""Tests for the recipient-confirmation route (follow-up to issue #94): `rtbf_agent.api.recipient`.

Mirrors `tests/api/test_approval.py`'s fixtures/structure -- see that file for the rationale behind
the `StaticPool` session fixture and the `TestClient` override.
"""

from __future__ import annotations

import ast
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from rtbf_agent.api.app import app
from rtbf_agent.api.deps import get_session
from rtbf_agent.core.models import Case, CaseState, Event, create_event

RECIPIENT_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "rtbf_agent" / "api" / "recipient.py"
)


@pytest.fixture
def session() -> Generator[Session, None, None]:
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
        with TestClient(app, follow_redirects=False) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session, None)


def _make_case(
    session: Session,
    *,
    state: CaseState = CaseState.DRAFT_READY,
    recipient_email: str | None = None,
) -> Case:
    case = Case(
        site="Example Corp",
        domain="example.com",
        target_email="user@example.com",
        recipient_email=recipient_email,
        state=state,
    )
    session.add(case)
    session.commit()
    session.refresh(case)
    return case


def _events(session: Session, case_id: int, kind: str) -> list[Event]:
    return list(
        session.exec(select(Event).where(Event.case_id == case_id, Event.kind == kind)).all()
    )


def test_confirm_recipient_sets_case_recipient_and_records_event(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)
    assert case.id is not None

    response = client.post(
        f"/cases/{case.id}/recipient", data={"recipient_email": "privacy@example.com"}
    )

    assert response.status_code == 303
    session.refresh(case)
    assert case.recipient_email == "privacy@example.com"
    events = _events(session, case.id, "recipient_confirmed")
    assert len(events) == 1
    assert events[0].actor == "human"
    assert events[0].detail == {"recipient": "privacy@example.com", "matched_suggestion": False}


def test_confirm_recipient_marks_matched_suggestion_true_when_it_matches_the_latest_proposal(
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

    response = client.post(
        f"/cases/{case.id}/recipient", data={"recipient_email": "contact@example.com"}
    )

    assert response.status_code == 303
    events = _events(session, case.id, "recipient_confirmed")
    assert len(events) == 1
    assert events[0].detail == {"recipient": "contact@example.com", "matched_suggestion": True}


def test_confirm_recipient_can_be_resubmitted_to_correct_a_typo(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session, recipient_email="wrong@example.com")
    assert case.id is not None

    response = client.post(
        f"/cases/{case.id}/recipient", data={"recipient_email": "right@example.com"}
    )

    assert response.status_code == 303
    session.refresh(case)
    assert case.recipient_email == "right@example.com"
    assert len(_events(session, case.id, "recipient_confirmed")) == 1


def test_confirm_recipient_refuses_once_the_case_is_already_approved(
    client: TestClient, session: Session
) -> None:
    """R1 (code-review finding): `approve_case()` leaves `case.state` at Draft ready so a separate
    CLI process can later act on the recorded `approval_granted` Event -- so `Draft ready` alone
    isn't a safe gate for a recipient change. Once approved, this route must refuse rather than let
    a human's approval decision silently apply to a since-changed, never-approved address."""
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

    response = client.post(
        f"/cases/{case.id}/recipient", data={"recipient_email": "different@example.com"}
    )

    assert response.status_code == 409
    session.refresh(case)
    assert case.recipient_email == "approved@example.com"  # untouched
    assert not _events(session, case.id, "recipient_confirmed")


@pytest.mark.parametrize("bad_value", ["   ", "not-an-email"])
def test_confirm_recipient_rejects_a_blank_or_at_less_value(
    client: TestClient, session: Session, bad_value: str
) -> None:
    # A truly empty value ("") is dropped by Starlette's own form parsing before it ever reaches
    # this route (`parse_qsl`'s default `keep_blank_values=False`), yielding FastAPI's generic 422
    # "Field required" rather than this route's own 400 -- so it isn't exercised here; whitespace-
    # only and no-`@` values below are the ones this route's own validation actually rejects.
    case = _make_case(session)
    assert case.id is not None

    response = client.post(f"/cases/{case.id}/recipient", data={"recipient_email": bad_value})

    assert response.status_code == 400
    session.refresh(case)
    assert case.recipient_email is None
    assert not _events(session, case.id, "recipient_confirmed")


@pytest.mark.parametrize(
    "state",
    [
        CaseState.DISCOVERED,
        CaseState.CLASSIFIED,
        CaseState.AWAITING_HUMAN_APPROVAL,
        CaseState.AWAITING_RESPONSE,
        CaseState.REQUIRES_HUMAN_ACTION,
        CaseState.DELETION_CONFIRMED,
        CaseState.REJECTED_ESCALATED,
        CaseState.CLOSED,
    ],
)
def test_confirm_recipient_only_available_for_cases_actually_in_draft_ready_state(
    client: TestClient, session: Session, state: CaseState
) -> None:
    case = _make_case(session, state=state)
    assert case.id is not None

    response = client.post(
        f"/cases/{case.id}/recipient", data={"recipient_email": "privacy@example.com"}
    )

    assert response.status_code == 409
    session.refresh(case)
    assert case.recipient_email is None


def test_confirm_recipient_404_for_an_unknown_case_id(client: TestClient) -> None:
    response = client.post("/cases/999999/recipient", data={"recipient_email": "a@example.com"})
    assert response.status_code == 404


# --------------------------------------------------------------------------------------------- #
# R23 -- every user-facing string this module produces is sourced from I18nProvider.translate().
# --------------------------------------------------------------------------------------------- #


def _string_literal_call_argument_violations(module_path: Path) -> list[str]:
    """Same check `test_approval.py` runs against `approval.py`: any literal string passed as
    `HTTPException`'s `detail` -- either the `detail=` keyword or the second positional argument
    (`HTTPException(status_code, detail, headers)`'s own signature order) -- that isn't itself an
    `I18nProvider.translate(...)` call. Checks both forms (OCR review finding): the keyword-only
    version only caught `detail=`, missing `HTTPException(400, "literal")`, a real blind spot even
    though no call site in this codebase uses that form today."""
    tree = ast.parse(module_path.read_text())
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if func_name != "HTTPException":
            continue
        for keyword in node.keywords:
            if keyword.arg != "detail":
                continue
            if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                violations.append(
                    f"line {node.lineno}: HTTPException(detail=...) got a literal string, "
                    "not an I18nProvider.translate(...) call"
                )
        if len(node.args) >= 2:
            positional_detail = node.args[1]
            if isinstance(positional_detail, ast.Constant) and isinstance(
                positional_detail.value, str
            ):
                violations.append(
                    f"line {node.lineno}: HTTPException(..., 'literal') got a literal string as "
                    "the positional detail argument, not an I18nProvider.translate(...) call"
                )
    return violations


def test_no_literal_user_facing_strings_in_recipient_route() -> None:
    assert _string_literal_call_argument_violations(RECIPIENT_MODULE_PATH) == []


def test_literal_string_violation_check_catches_a_real_keyword_violation(tmp_path: Path) -> None:
    bad_module = tmp_path / "bad_recipient.py"
    bad_module.write_text(
        "from fastapi import HTTPException\n"
        "def f():\n"
        '    raise HTTPException(status_code=404, detail="Case not found")\n'
    )
    assert _string_literal_call_argument_violations(bad_module)


def test_literal_string_violation_check_catches_a_real_positional_violation(tmp_path: Path) -> None:
    bad_module = tmp_path / "bad_recipient_positional.py"
    bad_module.write_text(
        "from fastapi import HTTPException\n"
        "def f():\n"
        '    raise HTTPException(404, "Case not found")\n'
    )
    assert _string_literal_call_argument_violations(bad_module)
