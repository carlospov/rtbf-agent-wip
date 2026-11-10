"""Tests for the kanban add-a-case route (issue #125's kanban half): `rtbf_agent.api.add_case`.

Mirrors `tests/api/test_case_overrides.py`'s structure exactly (R23 check helpers) plus a
route-level happy-path/error-path suite, since (unlike `case_overrides.py`, whose route behavior is
already covered end-to-end via `tests/api/test_case_detail.py`) this route has no other test module
already exercising it.
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
from rtbf_agent.api.deps import get_jurisdiction_detector, get_session
from rtbf_agent.core.models import Case, CaseState
from rtbf_agent.plugins.jurisdiction_detection.detector import JurisdictionAttempt

ADD_CASE_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "rtbf_agent" / "api" / "add_case.py"
)


def _string_literal_call_argument_violations(module_path: Path) -> list[str]:
    """Mirrors `tests/api/test_case_overrides.py`'s identical helper -- see its own docstring."""
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


def test_no_literal_user_facing_strings_in_add_case_route() -> None:
    assert _string_literal_call_argument_violations(ADD_CASE_MODULE_PATH) == []


def test_literal_string_violation_check_catches_a_real_keyword_violation(tmp_path: Path) -> None:
    bad_module = tmp_path / "bad_add_case.py"
    bad_module.write_text(
        "from fastapi import HTTPException\n"
        "def f():\n"
        '    raise HTTPException(status_code=400, detail="Could not add case")\n'
    )
    assert _string_literal_call_argument_violations(bad_module)


# --------------------------------------------------------------------------------------------- #
# Route behavior.
# --------------------------------------------------------------------------------------------- #


class _NullJurisdictionDetector:
    """No proposal, ever -- keeps the route's happy-path tests independent of jurisdiction
    detection's own behavior (already covered by `tests/test_cli.py`'s `add_case_for_url()`
    tests). `propose()` always returns a `JurisdictionAttempt`, never a bare `None` -- see that
    class's own docstring."""

    async def propose(self, *, site: str, domain: str) -> JurisdictionAttempt:
        return JurisdictionAttempt(query=f"crawl {site}", results=(), proposal=None)


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

    def _override_get_jurisdiction_detector() -> _NullJurisdictionDetector:
        return _NullJurisdictionDetector()

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_jurisdiction_detector] = _override_get_jurisdiction_detector
    try:
        with TestClient(app, follow_redirects=False) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_jurisdiction_detector, None)


def test_add_case_creates_a_case_and_redirects_to_its_detail_page(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RTBF_TARGET_EMAIL", "me@example.com")

    response = client.post("/cases", data={"url": "https://example.com/privacy"})

    assert response.status_code == 303
    case = session.exec(select(Case)).one()
    assert response.headers["location"] == f"/cases/{case.id}"
    assert case.domain == "example.com"
    assert case.state == CaseState.DISCOVERED


def test_add_case_dedupes_to_the_existing_case_for_the_same_domain(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RTBF_TARGET_EMAIL", "me@example.com")

    first = client.post("/cases", data={"url": "https://example.com/privacy"})
    second = client.post("/cases", data={"url": "https://example.com/signup"})

    assert first.headers["location"] == second.headers["location"]
    assert len(session.exec(select(Case)).all()) == 1


def test_add_case_without_target_email_configured_reports_a_clean_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RTBF_TARGET_EMAIL", raising=False)

    response = client.post("/cases", data={"url": "https://example.com"})

    assert response.status_code == 400


def test_add_case_with_an_unparseable_url_reports_a_clean_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RTBF_TARGET_EMAIL", "me@example.com")

    response = client.post("/cases", data={"url": "   "})

    assert response.status_code == 400
