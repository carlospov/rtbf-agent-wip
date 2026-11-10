"""Tests for the kanban edit-draft route (LLM-drafted-replies extension): `rtbf_agent.api.draft`.

Mirrors `tests/api/test_approval.py`'s structure exactly (fixtures, R23 check helpers) -- see that
module's own docstring for why.
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

DRAFT_MODULE_PATH = Path(__file__).resolve().parents[2] / "src" / "rtbf_agent" / "api" / "draft.py"


@pytest.fixture
def session() -> Generator[Session, None, None]:
    # StaticPool + check_same_thread=False: TestClient runs sync routes in a worker thread, a
    # different thread than this fixture -- mirrors tests/api/test_approval.py's fixture.
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
    draft: str = "Please delete my data.",
    subject: str = "Deletion request",
) -> Case:
    case = Case(
        site="Example Corp",
        domain="example.com",
        target_email="user@example.com",
        recipient_email="privacy@example-corp.com",
        state=state,
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
            detail={"draft": draft, "subject": subject},
        )
    )
    session.commit()
    return case


def _events(session: Session, case_id: int, kind: str) -> list[Event]:
    return list(
        session.exec(select(Event).where(Event.case_id == case_id, Event.kind == kind)).all()
    )


def test_kanban_edit_draft_records_a_new_draft_proposed_event(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)
    assert case.id is not None

    response = client.post(f"/cases/{case.id}/edit-draft", data={"body": "hand-edited body"})

    assert response.status_code == 303
    session.refresh(case)
    assert case.state == CaseState.DRAFT_READY  # unchanged
    draft_events = _events(session, case.id, "draft_proposed")
    assert len(draft_events) == 2  # the original, plus this edit's
    latest = max(draft_events, key=lambda event: event.id or 0)
    assert latest.detail["draft"] == "hand-edited body"
    assert latest.detail["subject"] == "Deletion request"  # defaulted, not blanked
    assert latest.actor == "human"


def test_kanban_edit_draft_404_for_an_unknown_case_id(client: TestClient) -> None:
    response = client.post("/cases/999999/edit-draft", data={"body": "hand-edited body"})
    assert response.status_code == 404


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
def test_kanban_edit_draft_only_available_for_draft_ready_cases(
    client: TestClient, session: Session, state: CaseState
) -> None:
    case = _make_case(session, state=state)
    assert case.id is not None

    response = client.post(f"/cases/{case.id}/edit-draft", data={"body": "hand-edited body"})

    assert response.status_code == 409
    session.refresh(case)
    assert case.state == state  # untouched by the refused edit attempt


def test_kanban_edit_draft_refused_once_the_draft_is_approved(
    client: TestClient, session: Session
) -> None:
    """R1: once approved, an edit must not silently apply to an already-authorized send."""
    case = _make_case(session)
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

    response = client.post(f"/cases/{case.id}/edit-draft", data={"body": "hand-edited body"})

    assert response.status_code == 409


def test_kanban_edit_draft_rejects_a_blank_body(client: TestClient, session: Session) -> None:
    case = _make_case(session)
    assert case.id is not None

    response = client.post(f"/cases/{case.id}/edit-draft", data={"body": "   "})

    assert response.status_code == 400


# --------------------------------------------------------------------------------------------- #
# R23 -- every user-facing string this module produces is sourced from I18nProvider.translate().
# --------------------------------------------------------------------------------------------- #


def _string_literal_call_argument_violations(module_path: Path) -> list[str]:
    """Mirrors `tests/api/test_approval.py`'s identical helper -- see its own docstring."""
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


def test_no_literal_user_facing_strings_in_draft_routes() -> None:
    assert _string_literal_call_argument_violations(DRAFT_MODULE_PATH) == []


def test_literal_string_violation_check_catches_a_real_keyword_violation(tmp_path: Path) -> None:
    bad_module = tmp_path / "bad_draft.py"
    bad_module.write_text(
        "from fastapi import HTTPException\n"
        "def f():\n"
        '    raise HTTPException(status_code=404, detail="Case not found")\n'
    )
    violations = _string_literal_call_argument_violations(bad_module)
    assert violations


def test_literal_string_violation_check_catches_a_real_positional_violation(
    tmp_path: Path,
) -> None:
    bad_module = tmp_path / "bad_draft_positional.py"
    bad_module.write_text(
        "from fastapi import HTTPException\n"
        "def f():\n"
        "    raise HTTPException(404, 'Case not found')\n"
    )
    assert _string_literal_call_argument_violations(bad_module)
