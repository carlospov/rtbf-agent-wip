"""Tests for the kanban approve/reject routes (issue #67): `rtbf_agent.api.approval`.

Includes the five tests issue #67's Verification section names, plus an R23 check that every
user-facing string this module produces goes through `I18nProvider.translate()`.
"""

from __future__ import annotations

import ast
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from rtbf_agent.agent_loop import send_request
from rtbf_agent.api.app import app
from rtbf_agent.api.deps import get_session
from rtbf_agent.core.interfaces import EmailMessage, Sender
from rtbf_agent.core.models import Case, CaseState, Event, create_event

APPROVAL_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "rtbf_agent" / "api" / "approval.py"
)


@pytest.fixture
def session() -> Generator[Session, None, None]:
    # StaticPool + check_same_thread=False: TestClient runs sync routes in a worker thread, a
    # different thread than this fixture -- mirrors tests/api/test_case_detail.py's fixture.
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


def _make_case(session: Session, *, state: CaseState = CaseState.DRAFT_READY) -> Case:
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
            detail={"draft": "Please delete my data.", "subject": "Deletion request"},
        )
    )
    session.commit()
    return case


def _events(session: Session, case_id: int, kind: str) -> list[Event]:
    return list(
        session.exec(select(Event).where(Event.case_id == case_id, Event.kind == kind)).all()
    )


class _FakeSender(Sender):
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


def test_kanban_approve_records_approval_granted_event_without_sending(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)
    assert case.id is not None

    response = client.post(f"/cases/{case.id}/approve")

    assert response.status_code == 303
    session.refresh(case)
    assert case.state == CaseState.DRAFT_READY  # unchanged -- this route never sends
    events = _events(session, case.id, "approval_granted")
    assert len(events) == 1
    assert events[0].actor == "human"
    assert not _events(session, case.id, "request_sent")


def test_kanban_reject_records_approval_rejected_event_and_moves_case_to_rejected_escalated(
    client: TestClient, session: Session
) -> None:
    case = _make_case(session)
    assert case.id is not None

    response = client.post(f"/cases/{case.id}/reject")

    assert response.status_code == 303
    session.refresh(case)
    assert case.state == CaseState.REJECTED_ESCALATED
    events = _events(session, case.id, "approval_rejected")
    assert len(events) == 1
    assert events[0].actor == "human"


async def test_cli_uses_already_recorded_kanban_approval_instead_of_reprompting(
    client: TestClient, session: Session
) -> None:
    """`send_request()` never solicits a decision itself (automated-case-loop rework -- see
    `core.interfaces.Notifier`'s module docstring); it only checks for an already-recorded
    `approval_granted` Event. A kanban approval, recorded by a genuinely separate process (this
    `TestClient` request), is exactly the kind of already-recorded decision the recurring loop's
    next `send_request()` call picks up and acts on."""
    case = _make_case(session)
    assert case.id is not None

    approve_response = client.post(f"/cases/{case.id}/approve")
    assert approve_response.status_code == 303

    sender = _FakeSender()
    result = await send_request(session, case, sender)

    assert result.state == CaseState.AWAITING_RESPONSE
    assert len(sender.sent) == 1
    assert len(_events(session, case.id, "request_sent")) == 1
    # Still exactly one approval_granted Event -- send_request() reused the kanban-recorded one
    # rather than creating a second.
    assert len(_events(session, case.id, "approval_granted")) == 1


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
def test_kanban_approve_reject_only_available_for_cases_actually_in_draft_ready_state(
    client: TestClient, session: Session, state: CaseState
) -> None:
    case = _make_case(session, state=state)
    assert case.id is not None

    approve_response = client.post(f"/cases/{case.id}/approve")
    assert approve_response.status_code == 409
    session.refresh(case)
    assert case.state == state  # untouched by the refused approve attempt

    reject_response = client.post(f"/cases/{case.id}/reject")
    assert reject_response.status_code == 409
    session.refresh(case)
    assert case.state == state  # untouched by the refused reject attempt


def test_kanban_approve_reject_404_for_an_unknown_case_id(client: TestClient) -> None:
    assert client.post("/cases/999999/approve").status_code == 404
    assert client.post("/cases/999999/reject").status_code == 404


# --------------------------------------------------------------------------------------------- #
# R23 -- every user-facing string this module produces is sourced from I18nProvider.translate().
# --------------------------------------------------------------------------------------------- #


def _string_literal_call_argument_violations(module_path: Path) -> list[str]:
    """Every call in `module_path` that passes a literal string where this module's only two
    user-facing-text call sites (`HTTPException(detail=...)`, `_i18n.translate(...)`'s own
    non-key arguments) could hide unsourced text -- a literal string passed as either the
    `detail=` keyword or the second positional argument to `HTTPException`
    (`HTTPException(status_code, detail, headers)`'s own signature order) that isn't itself an
    `_i18n.translate(...)` call. Checks both forms (OCR review finding on `test_recipient.py`'s
    identical helper, mirrored here for consistency): the keyword-only version missed
    `HTTPException(400, "literal")`, a real blind spot even though no call site in this codebase
    uses that form today.
    """
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


def test_no_literal_user_facing_strings_in_approval_routes() -> None:
    assert _string_literal_call_argument_violations(APPROVAL_MODULE_PATH) == []


def test_literal_string_violation_check_catches_a_real_keyword_violation(tmp_path: Path) -> None:
    bad_module = tmp_path / "bad_approval.py"
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
    bad_module = tmp_path / "bad_approval_positional.py"
    bad_module.write_text(
        "from fastapi import HTTPException\n"
        "def f():\n"
        '    raise HTTPException(404, "Case not found")\n'
    )
    assert _string_literal_call_argument_violations(bad_module)
