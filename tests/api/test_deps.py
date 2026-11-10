"""Tests for `rtbf_agent.api.deps` (issue #53): the DB session dependency reads `cli.py`'s DB.

The load-bearing guarantee here is "no second/independent database": a `Case` written through
`rtbf_agent.db.build_engine()` (the exact function `cli.py`'s `main_async()` calls) must be
visible through the API's `get_session` dependency when both resolve the same `DATABASE_URL` --
proving the API isn't quietly reading/writing some other store.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from sqlmodel import Session, select

from rtbf_agent.api import deps
from rtbf_agent.core.models import Case
from rtbf_agent.db import build_engine


@pytest.fixture(autouse=True)
def _reset_engine_cache() -> Generator[None, None, None]:
    """Every test gets a fresh cached engine, regardless of `DATABASE_URL` set by a prior test."""
    deps._reset_engine_cache()
    yield
    deps._reset_engine_cache()


def test_db_session_dependency_reads_the_same_database_cli_uses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A real on-disk SQLite file (not `:memory:`, which is only visible within the connection
    # that created it) shared by both "sides" via the same `DATABASE_URL`. `.as_posix()`, not
    # `str(tmp_path)`: on Windows the latter uses backslashes, which produce a malformed SQLite
    # URL when interpolated directly (SQLite URLs require forward slashes).
    db_path = f"{tmp_path.as_posix()}/shared.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    # "cli.py side": write a Case using the exact same `build_engine()` `main_async()` calls.
    cli_engine = build_engine()
    with Session(cli_engine) as cli_session:
        cli_session.add(Case(site="Example", domain="example.com", target_email="me@example.com"))
        cli_session.commit()

    # "api side": read it back through the dependency's own, independently-resolved engine.
    session_gen = deps.get_session()
    api_session = next(session_gen)
    try:
        cases = api_session.exec(select(Case)).all()
        assert len(cases) == 1
        assert cases[0].domain == "example.com"
    finally:
        session_gen.close()


def test_get_session_dependency_is_a_generator_yielding_exactly_one_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite://")

    gen = deps.get_session()
    session = next(gen)
    assert isinstance(session, Session)

    with pytest.raises(StopIteration):
        next(gen)
