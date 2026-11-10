"""Tests for `rtbf_agent.api.app` (issue #53): the FastAPI app starts and its DB plumbing works.

Uses `fastapi.testclient.TestClient` -- no real server process, per the issue's Verification
section. `app.dependency_overrides[get_session]` swaps in a `Session` bound to an isolated
in-memory SQLite engine for each test, so these tests never touch a real on-disk database.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from rtbf_agent.api.app import app
from rtbf_agent.api.deps import get_session
from rtbf_agent.core.models import Case


@pytest.fixture
def session() -> Generator[Session, None, None]:
    # `StaticPool` + `check_same_thread=False`: `TestClient` runs FastAPI's sync route handlers
    # in a worker thread, which is a different thread than this fixture. SQLite's default
    # `sqlite://` (in-memory) engine otherwise hands out a distinct connection per thread, so the
    # route would see a fresh, table-less in-memory DB instead of this fixture's one.
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


def test_api_app_starts_and_health_check_responds(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_check_does_not_require_a_db_session_override() -> None:
    """The health route has no DB dependency at all -- prove that with no override in place."""
    previous = app.dependency_overrides.pop(get_session, None)
    try:
        with TestClient(app) as test_client:
            response = test_client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    finally:
        if previous is not None:
            app.dependency_overrides[get_session] = previous


def test_case_count_reflects_the_overridden_session(client: TestClient, session: Session) -> None:
    response = client.get("/cases/count")
    assert response.json() == {"count": 0}

    session.add(Case(site="Example", domain="example.com", target_email="me@example.com"))
    session.commit()

    response = client.get("/cases/count")
    assert response.json() == {"count": 1}
