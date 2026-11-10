"""Shared DB engine/session configuration: the single source of truth for `DATABASE_URL`.

Both `cli.py` (the discover/classify/draft/send orchestration loop) and `api/` (the FastAPI
read-only kanban plumbing, issue #53) must read and write the exact same database -- there is no
separate "web" database and no separate config mechanism for it. This module exists so that's
enforced by construction rather than by convention: `resolve_database_url()`/`build_engine()` are
extracted out of `cli.py` (rather than `api/` importing `cli.py`'s private, leading-underscore
`_resolve_database_url()`, or worse, re-implementing the same `os.environ.get(...)` call a second
time) so there is exactly one function in the whole codebase that reads `DATABASE_URL`, and exactly
one that turns it into an `Engine`. `cli.py` re-exports both names for backwards compatibility with
anything already importing them from there.

`DATABASE_URL` (not `RTBF_`-prefixed) predates this project's `RTBF_` prefix convention -- see
`.env.example` and `cli.py`'s original module docstring -- and nothing yet has reason to rename it.
"""

from __future__ import annotations

import os

from sqlalchemy import Engine
from sqlmodel import SQLModel, create_engine

# Real report: `SQLModel.metadata` only knows about a table once the module defining it has been
# imported somewhere in the process -- every current real caller of `build_engine()` (`cli.py`,
# `api/app.py`) happens to import `core.models` transitively first, but nothing enforced that, and
# `_migrate_document_table_for_vault()` below reads `SQLModel.metadata.tables["document"]`
# directly: called before anything had imported `core.models` (e.g. a standalone script), it
# raised `KeyError` after already renaming the live table aside, leaving the database with no
# `document` table at all. Imported here, unconditionally, so `build_engine()` never depends on
# caller import order for this.
from rtbf_agent.core import models as _models  # noqa: F401

# Matches cli.py's long-standing default exactly -- this is not a new default, just a relocation.
DEFAULT_DATABASE_URL = "sqlite:///./rtbf-agent.db"


def resolve_database_url() -> str:
    """Read `DATABASE_URL`, falling back to the same on-disk SQLite default `cli.py` always used."""
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def _migrate_document_table_for_vault(engine: Engine) -> None:
    """One-time, idempotent fix-up for a `document` table created before issue #111 (the document
    vault) -- real report: this repo has no migration tooling, and `SQLModel.metadata.create_all()`
    only ever creates *missing* tables, never alters an existing one's columns/constraints. Without
    this, a database that already had a `document` table before #111 breaks outright the moment any
    code touches it: the new `Document.label` column doesn't exist yet (every query raises
    `OperationalError: no such column`), `ttl_expires_at` is still `NOT NULL` at the SQLite level
    even though the model now allows `None` (inserting a new vault document raises
    `IntegrityError`), and any row still carrying `DocumentStatus.ATTACHED`/`AWAITING_APPROVAL` --
    names #111 removed from the enum entirely -- fails to load at all (SQLAlchemy stores a Python
    enum column by its *name*, e.g. `"ATTACHED"`, not `.value`, so this isn't just a display-string
    mismatch).

    No-ops if `document` doesn't exist yet (a brand-new database -- `create_all()` below already
    creates it with the current, correct schema) or already has a `label` column (already migrated,
    or created fresh after #111). Otherwise: renames the old table aside, lets `create_all()` create
    a fresh one with the exact schema `SQLModel` currently defines (never hand-written SQL for the
    new table, so it can't drift from the real model), copies every row across -- remapping the two
    removed status names to their honest #111 equivalent (`ATTACHED` had already had its file
    secure-deleted under the old single-use model, so it becomes `PURGED`, never `AVAILABLE`, which
    would wrongly imply it can still be decrypted; `AWAITING_APPROVAL` was never destroyed, so it
    becomes `AVAILABLE`, exactly what it always effectively meant) -- then drops the old table.
    Matched case-insensitively (`UPPER(status)`) since not every existing value is guaranteed to be
    in the exact `.name` form (e.g. a value written by hand, matching `.value`'s title case
    instead) -- this normalizes any of those to the correct current member name too. Any value that
    matches none of the four known forms (a corrupted row, or a status from some even-older schema)
    falls back to `AVAILABLE` rather than being copied across verbatim -- a raw, unrecognized string
    would otherwise fail to load at all under the new two-member enum; `AVAILABLE` is the safer
    default of the two members since it's the one that doesn't claim the document's file was
    already destroyed.
    Fails loudly, rather than silently losing data, if a previous run of this exact migration was
    interrupted mid-way (process killed between the `ALTER TABLE ... RENAME` below and the final
    `DROP TABLE`): on a clean restart, `document` would look like a brand-new, unmigrated table (no
    `label` column, or missing entirely) while the real data actually still sits in
    `document_pre_111` -- proceeding would create a fresh, empty `document` table and orphan that
    data forever. Checked first, before the normal no-op/proceed decision below, since a leftover
    `document_pre_111` can only ever mean an interrupted previous run, never a legitimate state.
    """
    with engine.connect() as conn:
        if conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'document_pre_111'"
        ).fetchone():
            raise RuntimeError(
                "found a leftover 'document_pre_111' table -- a previous attempt at the issue "
                "#111 document-vault migration was interrupted before it finished. Refusing to "
                "proceed automatically to avoid silently losing its data: inspect "
                "'document_pre_111' by hand, restore it to 'document' (dropping whatever partial "
                "'document' table exists first), and re-run."
            )
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(document)").fetchall()}
        if not columns or "label" in columns:
            return
        conn.exec_driver_sql("ALTER TABLE document RENAME TO document_pre_111")
        SQLModel.metadata.create_all(conn, tables=[SQLModel.metadata.tables["document"]])
        conn.exec_driver_sql(
            """
            INSERT INTO document
                (id, case_id, status, label, encrypted_path, mime_type, size, sha256_hash,
                 received_at, ttl_expires_at)
            SELECT
                id, case_id,
                CASE UPPER(status)
                    WHEN 'ATTACHED' THEN 'PURGED'
                    WHEN 'AWAITING_APPROVAL' THEN 'AVAILABLE'
                    WHEN 'PURGED' THEN 'PURGED'
                    WHEN 'AVAILABLE' THEN 'AVAILABLE'
                    ELSE 'AVAILABLE'
                END,
                NULL, encrypted_path, mime_type, size, sha256_hash, received_at, ttl_expires_at
            FROM document_pre_111
            """
        )
        conn.exec_driver_sql("DROP TABLE document_pre_111")
        conn.commit()


def build_engine(database_url: str | None = None) -> Engine:
    """Create the SQLAlchemy engine for `database_url` (default: `resolve_database_url()`).

    Also ensures `SQLModel.metadata.create_all(engine)` has run, mirroring what `cli.py`'s
    `main_async()` already did inline -- callers (the CLI, the FastAPI app) never need to
    remember to call `create_all` themselves, and both end up with tables created against
    whichever engine they resolve, idempotently. `_migrate_document_table_for_vault()` runs first,
    since `create_all()` itself cannot fix up a `document` table that already exists with an
    outdated schema (issue #111) -- see that function's own docstring.
    """
    engine = create_engine(database_url if database_url is not None else resolve_database_url())
    _migrate_document_table_for_vault(engine)
    SQLModel.metadata.create_all(engine)
    return engine
