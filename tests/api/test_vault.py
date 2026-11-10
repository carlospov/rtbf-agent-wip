"""Tests for the document vault routes (issue #111): `rtbf_agent.api.vault`.

Mirrors `tests/api/test_document.py`'s fixtures/structure -- see that file for the rationale
behind the `StaticPool` session fixture and the `TestClient` override.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from rtbf_agent import document_store
from rtbf_agent.api.app import app
from rtbf_agent.api.deps import get_session
from rtbf_agent.core.models import Case, CaseState, Document, DocumentStatus, create_event

VAULT_MODULE_PATH = Path(__file__).resolve().parents[2] / "src" / "rtbf_agent" / "api" / "vault.py"
VAULT_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "rtbf_agent" / "api" / "templates" / "vault.html"
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

    app.dependency_overrides[get_session] = _override_get_session
    try:
        with TestClient(app, follow_redirects=False) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session, None)


def _make_case_requiring_human_action(session: Session, **overrides) -> Case:
    defaults = dict(
        site="Example Corp",
        domain="example.com",
        target_email="user@example.com",
        recipient_email="privacy@example-corp.com",
        state=CaseState.REQUIRES_HUMAN_ACTION,
    )
    defaults.update(overrides)
    case = Case(**defaults)
    session.add(case)
    session.commit()
    session.refresh(case)
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
    return case


def _upload_document(session: Session, case: Case, *, label: str | None = None) -> Document:
    document = document_store.store_document(
        session, case, b"identity document bytes", "image/png", label=label
    )
    session.commit()
    session.refresh(document)
    return document


def test_vault_page_lists_documents(client: TestClient, session: Session) -> None:
    case = _make_case_requiring_human_action(session)
    _upload_document(session, case, label="ID card")

    response = client.get("/vault")

    assert response.status_code == 200
    assert "ID card" in response.text


def test_vault_page_shows_empty_state(client: TestClient) -> None:
    response = client.get("/vault")
    assert response.status_code == 200


def test_delete_document_removes_it(client: TestClient, session: Session) -> None:
    case = _make_case_requiring_human_action(session)
    document = _upload_document(session, case)
    assert document.id is not None

    response = client.post(f"/vault/{document.id}/delete")

    assert response.status_code == 303
    session.refresh(document)
    assert document.status == DocumentStatus.PURGED


def test_delete_document_404_for_an_unknown_id(client: TestClient) -> None:
    response = client.post("/vault/999999/delete")
    assert response.status_code == 404


def test_download_document_returns_the_decrypted_bytes(
    client: TestClient, session: Session
) -> None:
    case = _make_case_requiring_human_action(session)
    document = _upload_document(session, case, label="ID card")
    assert document.id is not None

    response = client.get(f"/vault/{document.id}/download")

    assert response.status_code == 200
    assert response.content == b"identity document bytes"
    assert response.headers["content-type"] == "image/png"
    assert "ID card" in response.headers["content-disposition"]


def test_download_document_falls_back_to_the_id_placeholder_filename(
    client: TestClient, session: Session
) -> None:
    case = _make_case_requiring_human_action(session)
    document = _upload_document(session, case)
    assert document.id is not None

    response = client.get(f"/vault/{document.id}/download")

    assert response.status_code == 200
    assert f"document-{document.id}" in response.headers["content-disposition"]


def test_download_document_strips_backslash_from_the_filename(
    client: TestClient, session: Session
) -> None:
    """OCR review finding: a label ending in a backslash (the RFC 6266 quoted-string escape
    character) would otherwise turn the closing `"` of `filename="..."` into an escaped literal
    quote instead of terminating the header value."""
    case = _make_case_requiring_human_action(session)
    document = _upload_document(session, case, label="evil\\")
    assert document.id is not None

    response = client.get(f"/vault/{document.id}/download")

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert "\\" not in disposition
    assert disposition.count('"') == 2


def test_download_document_404_for_an_unknown_id(client: TestClient) -> None:
    response = client.get("/vault/999999/download")
    assert response.status_code == 404


def test_download_document_404_for_a_purged_document(client: TestClient, session: Session) -> None:
    case = _make_case_requiring_human_action(session)
    document = _upload_document(session, case)
    assert document.id is not None
    document_store.delete_vault_document(session, document)
    session.commit()

    response = client.get(f"/vault/{document.id}/download")

    assert response.status_code == 404


def test_rename_document_updates_the_label(client: TestClient, session: Session) -> None:
    case = _make_case_requiring_human_action(session)
    document = _upload_document(session, case, label="old name")
    assert document.id is not None

    response = client.post(f"/vault/{document.id}/rename", data={"label": "new name"})

    assert response.status_code == 303
    session.refresh(document)
    assert document.label == "new name"


def test_rename_document_rejects_a_blank_label(client: TestClient, session: Session) -> None:
    case = _make_case_requiring_human_action(session)
    document = _upload_document(session, case, label="old name")
    assert document.id is not None

    response = client.post(f"/vault/{document.id}/rename", data={"label": "   "})

    assert response.status_code == 400
    session.refresh(document)
    assert document.label == "old name"


def test_rename_document_404_for_an_unknown_id(client: TestClient) -> None:
    response = client.post("/vault/999999/rename", data={"label": "new name"})
    assert response.status_code == 404


def test_select_documents_records_the_selection(client: TestClient, session: Session) -> None:
    case = _make_case_requiring_human_action(session)
    assert case.id is not None
    document = _upload_document(session, case)
    assert document.id is not None

    response = client.post(
        f"/cases/{case.id}/select-documents", data={"document_ids": [document.id]}
    )

    assert response.status_code == 303
    from rtbf_agent.core.models import Event

    selections = session.exec(
        select(Event).where(Event.case_id == case.id, Event.kind == "documents_selected_for_resend")
    ).all()
    assert len(selections) == 1
    assert selections[0].detail["document_ids"] == [document.id]


def test_select_documents_allows_a_document_from_a_different_case(
    client: TestClient, session: Session
) -> None:
    """The whole point of the vault (issue #111): reuse across cases."""
    origin_case = _make_case_requiring_human_action(session)
    document = _upload_document(session, origin_case)
    assert document.id is not None
    target_case = _make_case_requiring_human_action(session, site="Other Corp")

    response = client.post(
        f"/cases/{target_case.id}/select-documents", data={"document_ids": [document.id]}
    )

    assert response.status_code == 303


def test_select_documents_rejects_an_unknown_id(client: TestClient, session: Session) -> None:
    case = _make_case_requiring_human_action(session)

    response = client.post(f"/cases/{case.id}/select-documents", data={"document_ids": [999999]})

    assert response.status_code == 400


def test_select_documents_refuses_outside_requires_human_action(
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

    response = client.post(f"/cases/{case.id}/select-documents", data={"document_ids": [1]})

    assert response.status_code == 409


def test_select_documents_404_for_an_unknown_case_id(client: TestClient) -> None:
    response = client.post("/cases/999999/select-documents", data={"document_ids": [1]})
    assert response.status_code == 404


# --------------------------------------------------------------------------------------------- #
# R23 -- every user-facing string this module produces is sourced from I18nProvider.translate().
# --------------------------------------------------------------------------------------------- #


def _string_literal_call_argument_violations(module_path: Path) -> list[str]:
    """Mirrors `test_document.py`'s identical helper."""
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


_TEXTUAL_ATTRS = ("alt", "title", "placeholder", "aria-label", "aria-valuetext")


def _strip_jinja(text: str) -> str:
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)
    text = re.sub(r"\{\{.*?\}\}", "", text, flags=re.DOTALL)
    text = re.sub(r"\{%.*?%\}", "", text, flags=re.DOTALL)
    return text


def _template_literal_text_violations(template_path: Path) -> list[str]:
    """Mirrors `test_case_detail.py`'s identical helper -- the docstring at the top of
    `vault.html` itself claims this scan already happens, so make that claim true rather than
    only checking `vault.py`'s `HTTPException` calls above."""
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


def test_no_literal_user_facing_strings_in_vault_routes() -> None:
    violations = _string_literal_call_argument_violations(
        VAULT_MODULE_PATH
    ) + _template_literal_text_violations(VAULT_TEMPLATE_PATH)
    assert violations == []


def test_literal_string_violation_check_catches_a_real_keyword_violation(tmp_path: Path) -> None:
    bad_module = tmp_path / "bad_vault.py"
    bad_module.write_text(
        "from fastapi import HTTPException\n"
        "def f():\n"
        '    raise HTTPException(status_code=404, detail="Document not found")\n'
    )
    assert _string_literal_call_argument_violations(bad_module)
