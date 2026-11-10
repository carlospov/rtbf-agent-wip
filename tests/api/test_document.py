"""Tests for the kanban additional-info (text/document) routes (automated-case-loop rework, Phase
5): `rtbf_agent.api.document`.

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
from rtbf_agent.api.deps import get_llm_model, get_llm_provider, get_session
from rtbf_agent.core.models import Case, CaseState, Document, DocumentStatus, Event, create_event
from rtbf_agent.plugins.llm.mock import MockLLMProvider

_TEST_MODEL = "test-model"
"""Stand-in for a real, resolved LLM model name -- `MockLLMProvider` ignores `request.model`
entirely, so any string works here; matches other test modules' identical constant."""

DOCUMENT_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "rtbf_agent" / "api" / "document.py"
)


@pytest.fixture
def session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(autouse=True)
def _document_env(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RTBF_DOCUMENT_ENCRYPTION_KEY", "zJ8gk8h1qYw3F1v3l4v0m9C7b3e9r0g5t9p9nQe8s0A="
    )
    monkeypatch.setenv("RTBF_DOCUMENT_STORAGE_DIR", str(tmp_path / "documents"))


@pytest.fixture
def client(session: Session) -> Generator[TestClient, None, None]:
    def _override_get_session() -> Generator[Session, None, None]:
        yield session

    # The draft route (below) needs a real LLMProvider/model to compose text on demand --
    # overridden here with a MockLLMProvider/fixed model string, the same
    # `app.dependency_overrides` mechanism `get_session` already uses, rather than a real network
    # call or env-var-dependent resolution (see `api/deps.py`'s `get_llm_provider()`/
    # `get_llm_model()` docstrings for why these two exist as separate, overridable dependencies).
    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_llm_provider] = lambda: MockLLMProvider()
    app.dependency_overrides[get_llm_model] = lambda: _TEST_MODEL
    try:
        with TestClient(app, follow_redirects=False) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_llm_provider, None)
        app.dependency_overrides.pop(get_llm_model, None)


def _make_case_requiring_human_action(session: Session) -> Case:
    case = Case(
        site="Example Corp",
        domain="example.com",
        target_email="user@example.com",
        recipient_email="privacy@example-corp.com",
        state=CaseState.REQUIRES_HUMAN_ACTION,
    )
    session.add(case)
    session.commit()
    session.refresh(case)
    assert case.id is not None
    # An original draft, as if `agent_loop.send_request()` sent it once before this reply came in --
    # `agent_loop.compose_additional_info_draft()`'s `latest_draft()` lookup (for the subject) needs
    # one to exist, same as a real case reaching `Requires human action` always has.
    session.add(
        create_event(
            case_id=case.id,
            kind="draft_proposed",
            actor="agent",
            detail={
                "draft": "Please delete my personal data.",
                "subject": "Deletion request for example.com",
            },
        )
    )
    session.add(
        create_event(
            case_id=case.id,
            kind="additional_info_requested",
            actor="agent",
            detail={"reasoning": "site asked for date of birth"},
        )
    )
    session.commit()
    return case


def _events(session: Session, case_id: int, kind: str) -> list[Event]:
    return list(
        session.exec(select(Event).where(Event.case_id == case_id, Event.kind == kind)).all()
    )


def test_provide_text_response_records_the_answer(client: TestClient, session: Session) -> None:
    case = _make_case_requiring_human_action(session)
    assert case.id is not None

    response = client.post(
        f"/cases/{case.id}/text-response", data={"text": "my date of birth is 1990-01-01"}
    )

    assert response.status_code == 303
    events = _events(session, case.id, "additional_info_text_provided")
    assert len(events) == 1
    assert events[0].detail == {"text": "my date of birth is 1990-01-01"}


def test_provide_text_response_rejects_blank_text(client: TestClient, session: Session) -> None:
    case = _make_case_requiring_human_action(session)
    assert case.id is not None

    response = client.post(f"/cases/{case.id}/text-response", data={"text": "   "})

    assert response.status_code == 400
    assert _events(session, case.id, "additional_info_text_provided") == []


def test_provide_text_response_refuses_outside_requires_human_action(
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

    response = client.post(f"/cases/{case.id}/text-response", data={"text": "here you go"})

    assert response.status_code == 409


def test_provide_text_response_404_for_an_unknown_case_id(client: TestClient) -> None:
    response = client.post("/cases/999999/text-response", data={"text": "here you go"})
    assert response.status_code == 404


def test_draft_additional_info_records_a_preview_without_changing_state(
    client: TestClient, session: Session
) -> None:
    """The drafting-is-a-preview redesign (superseding issue #117/#119's fused "this round is
    ready" draft+send route): composing a preview from what's on record leaves `case.state` at
    `Requires human action` -- distinct from the text/document routes above, which never trigger a
    resend either, and distinct from the old fused route this one replaces, which sent
    immediately."""
    case = _make_case_requiring_human_action(session)
    assert case.id is not None
    client.post(f"/cases/{case.id}/text-response", data={"text": "my date of birth is 1990-01-01"})

    response = client.post(f"/cases/{case.id}/additional-info/draft")

    assert response.status_code == 303
    events = _events(session, case.id, "draft_proposed")
    assert len(events) == 2  # the fixture's original draft, plus this preview
    latest = max(events, key=lambda event: event.id or 0)
    assert latest.actor == "agent"
    assert "1990-01-01" in latest.detail["draft"]
    session.refresh(case)
    assert case.state == CaseState.REQUIRES_HUMAN_ACTION
    assert _events(session, case.id, "approval_granted") == []


def test_draft_additional_info_rejects_without_text_or_documents(
    client: TestClient, session: Session
) -> None:
    case = _make_case_requiring_human_action(session)
    assert case.id is not None

    response = client.post(f"/cases/{case.id}/additional-info/draft")

    assert response.status_code == 400
    assert len(_events(session, case.id, "draft_proposed")) == 1  # only the fixture's original


def test_draft_additional_info_refuses_outside_requires_human_action(
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

    response = client.post(f"/cases/{case.id}/additional-info/draft")

    assert response.status_code == 409


def test_draft_additional_info_404_for_an_unknown_case_id(client: TestClient) -> None:
    response = client.post("/cases/999999/additional-info/draft")
    assert response.status_code == 404


def test_attach_document_records_the_answer(client: TestClient, session: Session) -> None:
    case = _make_case_requiring_human_action(session)
    assert case.id is not None

    response = client.post(
        f"/cases/{case.id}/document",
        files={"documents": ("id.png", b"identity document bytes", "image/png")},
    )

    assert response.status_code == 303
    documents = session.exec(select(Document)).all()
    assert len(documents) == 1
    assert documents[0].status == DocumentStatus.AVAILABLE
    assert documents[0].mime_type == "image/png"
    assert len(_events(session, case.id, "document_received")) == 1


def test_attach_document_accepts_multiple_files(client: TestClient, session: Session) -> None:
    case = _make_case_requiring_human_action(session)
    assert case.id is not None

    response = client.post(
        f"/cases/{case.id}/document",
        files=[
            ("documents", ("front.png", b"id front bytes", "image/png")),
            ("documents", ("back.png", b"id back bytes", "image/png")),
        ],
    )

    assert response.status_code == 303
    documents = session.exec(select(Document)).all()
    assert len(documents) == 2
    assert {document.mime_type for document in documents} == {"image/png"}
    assert len(_events(session, case.id, "document_received")) == 2


def test_attach_document_with_a_shared_label_suffixes_per_file(
    client: TestClient, session: Session
) -> None:
    case = _make_case_requiring_human_action(session)
    assert case.id is not None

    response = client.post(
        f"/cases/{case.id}/document",
        data={"label": "ID card"},
        files=[
            ("documents", ("front.png", b"id front bytes", "image/png")),
            ("documents", ("back.png", b"id back bytes", "image/png")),
        ],
    )

    assert response.status_code == 303
    labels = sorted(document.label for document in session.exec(select(Document)).all())
    assert labels == ["ID card (1)", "ID card (2)"]


def test_attach_document_with_a_label_and_a_single_file_does_not_suffix(
    client: TestClient, session: Session
) -> None:
    case = _make_case_requiring_human_action(session)
    assert case.id is not None

    response = client.post(
        f"/cases/{case.id}/document",
        data={"label": "Proof of address"},
        files={"documents": ("proof.pdf", b"proof of address bytes", "application/pdf")},
    )

    assert response.status_code == 303
    document = session.exec(select(Document)).one()
    assert document.label == "Proof of address"


def test_attach_document_multi_file_whole_request_fails_together(
    client: TestClient, session: Session
) -> None:
    """One oversized file in the batch fails the whole request -- none of the batch is recorded,
    including the valid file(s) before it."""
    from rtbf_agent.api.document import _MAX_DOCUMENT_BYTES

    case = _make_case_requiring_human_action(session)
    assert case.id is not None
    oversized = b"x" * (_MAX_DOCUMENT_BYTES + 1)

    response = client.post(
        f"/cases/{case.id}/document",
        files=[
            ("documents", ("front.png", b"id front bytes", "image/png")),
            ("documents", ("back.png", oversized, "image/png")),
        ],
    )

    assert response.status_code == 413
    assert session.exec(select(Document)).all() == []


def test_attach_document_rejects_empty_content(client: TestClient, session: Session) -> None:
    case = _make_case_requiring_human_action(session)
    assert case.id is not None

    response = client.post(
        f"/cases/{case.id}/document",
        files={"documents": ("id.png", b"", "image/png")},
    )

    assert response.status_code == 400
    assert session.exec(select(Document)).all() == []


def test_attach_document_rejects_content_over_the_size_limit(
    client: TestClient, session: Session
) -> None:
    """OCR review finding: an unbounded read would let a client make this process allocate/
    encrypt/write arbitrarily large content -- rejected with 413 before ever reaching
    `case_actions.attach_document()`/`document_store.store_document()`."""
    from rtbf_agent.api.document import _MAX_DOCUMENT_BYTES

    case = _make_case_requiring_human_action(session)
    assert case.id is not None
    oversized = b"x" * (_MAX_DOCUMENT_BYTES + 1)

    response = client.post(
        f"/cases/{case.id}/document",
        files={"documents": ("id.png", oversized, "image/png")},
    )

    assert response.status_code == 413
    assert session.exec(select(Document)).all() == []


def test_attach_document_refuses_outside_requires_human_action(
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

    response = client.post(
        f"/cases/{case.id}/document",
        files={"documents": ("id.png", b"identity document bytes", "image/png")},
    )

    assert response.status_code == 409


def test_attach_document_404_for_an_unknown_case_id(client: TestClient) -> None:
    response = client.post(
        "/cases/999999/document",
        files={"documents": ("id.png", b"identity document bytes", "image/png")},
    )
    assert response.status_code == 404


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


def test_no_literal_user_facing_strings_in_document_routes() -> None:
    assert _string_literal_call_argument_violations(DOCUMENT_MODULE_PATH) == []


def test_literal_string_violation_check_catches_a_real_keyword_violation(tmp_path: Path) -> None:
    bad_module = tmp_path / "bad_document.py"
    bad_module.write_text(
        "from fastapi import HTTPException\n"
        "def f():\n"
        '    raise HTTPException(status_code=404, detail="Case not found")\n'
    )
    assert _string_literal_call_argument_violations(bad_module)
