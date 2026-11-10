"""Tests for the kanban follow-up confirm/dispute routes (automated-case-loop rework, Phase 5):
`rtbf_agent.api.followup`.

Mirrors `tests/api/test_approval.py`'s fixtures/structure -- see that file for the rationale
behind the `StaticPool` session fixture and the `TestClient` override.
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

FOLLOWUP_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "rtbf_agent" / "api" / "followup.py"
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


def _make_case_with_pending_classification(session: Session, *, intent: str = "rejection") -> Case:
    case = Case(
        site="Example Corp",
        domain="example.com",
        target_email="user@example.com",
        recipient_email="privacy@example-corp.com",
        state=CaseState.AWAITING_RESPONSE,
    )
    session.add(case)
    session.commit()
    session.refresh(case)
    assert case.id is not None
    session.add(create_event(case_id=case.id, kind="request_sent", actor="agent"))
    session.add(
        create_event(
            case_id=case.id,
            kind="response_classified",
            actor="agent",
            detail={"message_id": "reply-1", "intent": intent, "reasoning": "Looks clear."},
        )
    )
    session.commit()
    return case


def _events(session: Session, case_id: int, kind: str) -> list[Event]:
    return list(
        session.exec(select(Event).where(Event.case_id == case_id, Event.kind == kind)).all()
    )


def test_confirm_followup_refuses_a_pending_confirmation_intent(
    client: TestClient, session: Session
) -> None:
    """Maintainer-directed narrowing of issue #68 (see `followup.py`'s module docstring, "Why
    CONFIRMATION auto-closes now"): `follow_up()` now resolves a CONFIRMATION classification
    itself, so this route's actionable set no longer includes it -- a case sitting on one (e.g. a
    stale fixture, or somehow left over) is refused, not silently closed by this route."""
    case = _make_case_with_pending_classification(session, intent="confirmation")
    assert case.id is not None

    response = client.post(f"/cases/{case.id}/followup/confirm")

    assert response.status_code == 409
    session.refresh(case)
    assert case.state == CaseState.AWAITING_RESPONSE


def test_confirm_followup_closes_a_rejection_intent_to_rejected_escalated(
    client: TestClient, session: Session
) -> None:
    case = _make_case_with_pending_classification(session, intent="rejection")
    assert case.id is not None

    response = client.post(f"/cases/{case.id}/followup/confirm")

    assert response.status_code == 303
    session.refresh(case)
    assert case.state == CaseState.REJECTED_ESCALATED
    assert len(_events(session, case.id, "followup_confirmed")) == 1


def test_dispute_followup_moves_case_to_requires_human_action(
    client: TestClient, session: Session
) -> None:
    case = _make_case_with_pending_classification(session)
    assert case.id is not None

    response = client.post(f"/cases/{case.id}/followup/dispute")

    assert response.status_code == 303
    session.refresh(case)
    assert case.state == CaseState.REQUIRES_HUMAN_ACTION
    assert len(_events(session, case.id, "followup_disputed")) == 1


def test_confirm_followup_refuses_when_nothing_is_pending(
    client: TestClient, session: Session
) -> None:
    case = Case(
        site="Example",
        domain="example.com",
        target_email="user@example.com",
        state=CaseState.AWAITING_RESPONSE,
    )
    session.add(case)
    session.commit()
    session.refresh(case)

    response = client.post(f"/cases/{case.id}/followup/confirm")

    assert response.status_code == 409


def test_dispute_followup_refuses_when_nothing_is_pending(
    client: TestClient, session: Session
) -> None:
    case = Case(
        site="Example",
        domain="example.com",
        target_email="user@example.com",
        state=CaseState.AWAITING_RESPONSE,
    )
    session.add(case)
    session.commit()
    session.refresh(case)

    response = client.post(f"/cases/{case.id}/followup/dispute")

    assert response.status_code == 409


def test_confirm_followup_404_for_an_unknown_case_id(client: TestClient) -> None:
    assert client.post("/cases/999999/followup/confirm").status_code == 404


def test_dispute_followup_404_for_an_unknown_case_id(client: TestClient) -> None:
    assert client.post("/cases/999999/followup/dispute").status_code == 404


# --------------------------------------------------------------------------------------------- #
# reopen -- the mitigating control for follow_up()'s CONFIRMATION auto-close.
# --------------------------------------------------------------------------------------------- #


def test_reopen_case_moves_deletion_confirmed_to_requires_human_action(
    client: TestClient, session: Session
) -> None:
    case = Case(
        site="Example",
        domain="example.com",
        target_email="user@example.com",
        state=CaseState.DELETION_CONFIRMED,
    )
    session.add(case)
    session.commit()
    session.refresh(case)

    response = client.post(f"/cases/{case.id}/followup/reopen")

    assert response.status_code == 303
    session.refresh(case)
    assert case.state == CaseState.REQUIRES_HUMAN_ACTION
    assert len(_events(session, case.id, "case_reopened")) == 1


def test_reopen_case_refuses_a_case_not_deletion_confirmed(
    client: TestClient, session: Session
) -> None:
    case = Case(
        site="Example",
        domain="example.com",
        target_email="user@example.com",
        state=CaseState.AWAITING_RESPONSE,
    )
    session.add(case)
    session.commit()
    session.refresh(case)

    response = client.post(f"/cases/{case.id}/followup/reopen")

    assert response.status_code == 409
    session.refresh(case)
    assert case.state == CaseState.AWAITING_RESPONSE


def test_reopen_case_404_for_an_unknown_case_id(client: TestClient) -> None:
    assert client.post("/cases/999999/followup/reopen").status_code == 404


# --------------------------------------------------------------------------------------------- #
# R23 -- every user-facing string this module produces is sourced from I18nProvider.translate().
# --------------------------------------------------------------------------------------------- #


def _string_literal_call_argument_violations(module_path: Path) -> list[str]:
    """Mirrors `test_approval.py`/`test_recipient.py`'s identical helper."""
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


def test_no_literal_user_facing_strings_in_followup_routes() -> None:
    assert _string_literal_call_argument_violations(FOLLOWUP_MODULE_PATH) == []


def test_literal_string_violation_check_catches_a_real_keyword_violation(tmp_path: Path) -> None:
    bad_module = tmp_path / "bad_followup.py"
    bad_module.write_text(
        "from fastapi import HTTPException\n"
        "def f():\n"
        '    raise HTTPException(status_code=404, detail="Case not found")\n'
    )
    assert _string_literal_call_argument_violations(bad_module)
