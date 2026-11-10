"""Tests for `rtbf_agent.db`: the shared `DATABASE_URL` resolution + engine construction.

Covers the two things that matter for issue #53's "no second database" requirement: that
`resolve_database_url()` reads the same env var/default `cli.py` always has, and that
`build_engine()` actually creates all `SQLModel` tables against whatever engine it builds.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine as sa_create_engine
from sqlmodel import Session, select

from rtbf_agent.core.models import Case, Document, DocumentStatus
from rtbf_agent.db import DEFAULT_DATABASE_URL, build_engine, resolve_database_url


def test_resolve_database_url_falls_back_to_default_when_env_var_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert resolve_database_url() == DEFAULT_DATABASE_URL


def test_resolve_database_url_reads_the_env_var_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./somewhere-else.db")

    assert resolve_database_url() == "sqlite:///./somewhere-else.db"


def test_build_engine_creates_all_tables_and_is_immediately_queryable() -> None:
    engine = build_engine("sqlite://")  # in-memory, isolated per call

    with Session(engine) as session:
        # If `create_all` hadn't run, this would raise `OperationalError: no such table: case`.
        assert session.exec(select(Case)).all() == []


def test_build_engine_defaults_to_resolve_database_url_when_no_url_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite://")

    engine = build_engine()

    assert str(engine.url) == "sqlite://"


# --------------------------------------------------------------------------------------------- #
# _migrate_document_table_for_vault (issue #111): a pre-#111 `document` table must keep working.
# --------------------------------------------------------------------------------------------- #


def _pre_111_engine(tmp_path):
    """A SQLite file with a `document` table shaped exactly like it was before issue #111 (no
    `label` column, `ttl_expires_at NOT NULL`) plus a `case` row and one document row per legacy
    status name -- `SQLAlchemy` stores a Python enum column by its *member name*
    (`"AWAITING_APPROVAL"`/`"ATTACHED"`), not `.value`, which is what actually breaks without this
    migration."""
    db_path = tmp_path / "pre111.db"
    engine = sa_create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.connect() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE "case" (
                id INTEGER PRIMARY KEY,
                site VARCHAR NOT NULL,
                domain VARCHAR NOT NULL,
                target_email VARCHAR NOT NULL,
                state VARCHAR NOT NULL DEFAULT 'DISCOVERED',
                recipient_email VARCHAR,
                applicable_jurisdiction VARCHAR,
                drafting_language VARCHAR,
                discovered_at TIMESTAMP NOT NULL,
                discovery_evidence VARCHAR,
                updated_at TIMESTAMP NOT NULL,
                reminder_enabled BOOLEAN,
                reminder_interval_days INTEGER
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE document (
                id INTEGER PRIMARY KEY,
                case_id INTEGER NOT NULL REFERENCES "case" (id),
                status VARCHAR NOT NULL,
                encrypted_path VARCHAR NOT NULL,
                mime_type VARCHAR NOT NULL,
                size INTEGER NOT NULL,
                sha256_hash VARCHAR NOT NULL,
                received_at TIMESTAMP NOT NULL,
                ttl_expires_at TIMESTAMP NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            'INSERT INTO "case" (id, site, domain, target_email, discovered_at, updated_at) '
            "VALUES (1, 'Example', 'example.com', 'user@example.com', "
            "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        # "Purged" (title case, matching the old enum's .value) alongside the other rows' proper
        # .name form ("AWAITING_APPROVAL"/"ATTACHED") -- real report: a value written by hand
        # (e.g. a one-off manual DB fix) doesn't necessarily match the .name form SQLAlchemy itself
        # always writes; the migration must normalize either.
        for row_id, status in enumerate(["AWAITING_APPROVAL", "ATTACHED", "Purged"], start=1):
            conn.exec_driver_sql(
                "INSERT INTO document "
                "(id, case_id, status, encrypted_path, mime_type, size, sha256_hash, "
                "received_at, ttl_expires_at) VALUES "
                f"({row_id}, 1, '{status}', '/tmp/doc-{row_id}.enc', 'image/png', 10, "
                "'deadbeef', '2026-01-01T00:00:00', '2026-01-02T00:00:00')"
            )
        conn.commit()
    return db_path


def test_build_engine_migrates_a_pre_111_document_table(tmp_path) -> None:
    db_path = _pre_111_engine(tmp_path)

    engine = build_engine(f"sqlite:///{db_path.as_posix()}")

    with Session(engine) as session:
        documents = sorted(session.exec(select(Document)).all(), key=lambda document: document.id)
        assert [document.status for document in documents] == [
            DocumentStatus.AVAILABLE,  # was AWAITING_APPROVAL -- never destroyed, still usable
            DocumentStatus.PURGED,  # was ATTACHED -- its file was already secure-deleted
            DocumentStatus.PURGED,  # was already PURGED
        ]
        assert all(document.label is None for document in documents)
        # Real report (OCR review): a status/label-only assertion wouldn't catch the migration's
        # INSERT ... SELECT accidentally dropping or nulling ttl_expires_at -- assert it survives.
        assert all(
            document.ttl_expires_at == datetime(2026, 1, 2, tzinfo=UTC) for document in documents
        )


def test_build_engine_can_insert_a_document_with_no_ttl_after_migrating(tmp_path) -> None:
    """The real bug this migration fixes: a pre-#111 `ttl_expires_at NOT NULL` column would raise
    `IntegrityError` the moment a new vault document (no TTL) is inserted."""
    db_path = _pre_111_engine(tmp_path)
    engine = build_engine(f"sqlite:///{db_path.as_posix()}")

    with Session(engine) as session:
        case = session.get(Case, 1)
        assert case is not None
        document = Document(
            case_id=1,
            status=DocumentStatus.AVAILABLE,
            encrypted_path="/tmp/new.enc",
            mime_type="image/png",
            size=1,
            sha256_hash="cafebabe",
        )
        session.add(document)
        session.commit()  # must not raise


def test_build_engine_is_a_noop_on_an_already_migrated_database(tmp_path) -> None:
    db_path = tmp_path / "fresh.db"
    build_engine(f"sqlite:///{db_path.as_posix()}")  # creates a fresh, already-current schema

    # A second call must not error (no `document_pre_111` table left over from a real migration
    # to collide with, no already-`label`-having table mistaken for needing one).
    build_engine(f"sqlite:///{db_path.as_posix()}")


def test_build_engine_refuses_to_proceed_past_an_interrupted_migration(tmp_path) -> None:
    """OCR review finding: a leftover `document_pre_111` table (a previous migration run killed
    between the `ALTER TABLE ... RENAME` and the final `DROP TABLE`) must fail loudly, not be
    silently treated as "nothing to migrate" -- that would create a fresh, empty `document` table
    and orphan the real data in `document_pre_111` forever."""
    db_path = _pre_111_engine(tmp_path)
    engine = sa_create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.connect() as conn:
        conn.exec_driver_sql("ALTER TABLE document RENAME TO document_pre_111")
        conn.commit()

    with pytest.raises(RuntimeError, match="document_pre_111"):
        build_engine(f"sqlite:///{db_path.as_posix()}")
