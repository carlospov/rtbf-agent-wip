"""FastAPI application: read-only plumbing for the kanban UI (issue #53, foundation for #54/#55).

**Frontend approach (explicit decision, resolving #18's open question):** server-rendered HTML via
Jinja2 templates (`fastapi.templating.Jinja2Templates`), not a separate SPA/build-step frontend.
Reasons:

- This project's stated ethos (`README.md`) is "no centralized backend, minimal footprint,
  self-hosted" -- a single operator runs this on their own machine. A build-step SPA (React/Vue +
  a bundler) adds a whole second toolchain (node, a package manager, a build step, a dev proxy)
  for a UI that is fundamentally a handful of read-mostly kanban columns and a case-detail page --
  disproportionate to the actual UI complexity.
- Jinja2 has first-class support in FastAPI (`fastapi.templating.Jinja2Templates`) with no extra
  framework glue, and server-rendered HTML is trivially cacheable/inspectable/curl-able, which
  matters for a project whose whole pitch is transparency and auditability.
- The one real interactive piece -- approving/rejecting a case -- is explicitly *out of scope*
  for this sub-issue and its siblings (#54/#55); the spec's primary approval surface is Telegram
  (see the parent tracking issue). Even when a web approve/reject action is added later, an HTML
  `<form>` POST handled by a FastAPI route is enough; nothing here requires client-side state
  management that would justify an SPA.

Issue #53 (the original version of this module) deliberately did **not** add `jinja2` or wire up
`Jinja2Templates`: there was no rendered view in scope yet. Issue #54 (kanban board) adds both,
plus the `templates/` directory and the actual kanban route -- see `rtbf_agent.api.kanban` for
that route, its query, and its i18n resolution; this module only mounts it via
`app.include_router(...)` and redirects the bare root to it. Issue #55 (case detail / event audit
trail) adds a second route the same way -- see `rtbf_agent.api.case_detail` for its query, i18n
resolution, and why its path uses Starlette's `{case_id:int}` convertor rather than a bare
`{case_id}` (so it can never swallow the literal `/cases/count` route defined below, regardless of
router registration order).

**What the routes defined directly in *this* module are** is a proof the plumbing works end-to-
end: a health-check route with no DB dependency, and a `Case` count route that goes through
`get_session` (`rtbf_agent.api.deps`) -- the same `Session`/engine machinery `cli.py` uses via
`rtbf_agent.db` (see that module's docstring for why there is exactly one `DATABASE_URL`
resolution path in this codebase).

Run locally: `uv run uvicorn rtbf_agent.api:app --reload` (see `README.md`).

**`.env` loading (issue #84):** `uvicorn` imports this module directly (via `rtbf_agent.api:app`)
rather than going through a custom entrypoint function, so this is the one place to load `.env`
for the API server, mirroring `rtbf_agent.__main__`'s identical rationale for the CLI -- both are
"the module a real invocation actually imports first," not a shared helper, since there is no
third module both already import early enough to hook into. Runs before any of this module's own
config-reading imports (`api.deps`/`api.kanban`/etc. read `DATABASE_URL`/`INTERFACE_LANGUAGE`
lazily inside request handlers, not at import time, but loading `.env` first here removes any
doubt). Never overrides an already-exported real environment variable (`load_dotenv()`'s own
default, `override=False`).
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402
from sqlmodel import Session, func, select  # noqa: E402

from rtbf_agent.api.add_case import router as add_case_router  # noqa: E402
from rtbf_agent.api.approval import router as approval_router  # noqa: E402
from rtbf_agent.api.case_detail import router as case_detail_router  # noqa: E402
from rtbf_agent.api.case_overrides import router as case_overrides_router  # noqa: E402
from rtbf_agent.api.deps import get_session  # noqa: E402
from rtbf_agent.api.document import router as document_router  # noqa: E402
from rtbf_agent.api.draft import router as draft_router  # noqa: E402
from rtbf_agent.api.followup import router as followup_router  # noqa: E402
from rtbf_agent.api.kanban import router as kanban_router  # noqa: E402
from rtbf_agent.api.recipient import router as recipient_router  # noqa: E402
from rtbf_agent.api.vault import router as vault_router  # noqa: E402
from rtbf_agent.core.models import Case  # noqa: E402

app = FastAPI(
    title="RTBF-Agent",
    description="Human-in-the-loop right-to-be-forgotten case manager -- local kanban UI.",
)
app.include_router(kanban_router)
app.include_router(add_case_router)
app.include_router(case_detail_router)
app.include_router(approval_router)
app.include_router(recipient_router)
app.include_router(case_overrides_router)
app.include_router(followup_router)
app.include_router(document_router)
app.include_router(draft_router)
app.include_router(vault_router)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Redirect the bare root to the kanban board.

    Carries no user-facing text of its own (a redirect has no body a human reads), so nothing here
    needs an i18n lookup -- unlike every route in `rtbf_agent.api.kanban`.
    """
    return RedirectResponse(url="/cases")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check with no DB dependency -- proves the app itself starts and serves requests."""
    return {"status": "ok"}


@app.get("/cases/count")
def case_count(session: Session = Depends(get_session)) -> dict[str, int]:
    """Bare proof the DB session dependency works: total `Case` row count, no rendering yet.

    A `SELECT COUNT(...)` at the database level, not `len(session.exec(select(Case)).all())` --
    the latter would materialize every `Case` row (and every column on it) into Python objects just
    to discard them, an anti-pattern that costs nothing to avoid here and shouldn't get copied into
    a real kanban query later.

    `session.scalar(...)` rather than `session.exec(...).one()`: `exec()` only auto-unwraps to a
    bare scalar for single-*entity* selects (SQLModel's own convenience layer over SQLAlchemy) --
    for an aggregate column expression like `func.count()` that unwrapping is SQLModel-version-
    dependent behavior, not a documented contract. `session.scalar()` is plain SQLAlchemy, always
    returns the single scalar value for a column-expression query, and isn't ambiguous to a reader
    unfamiliar with SQLModel's `exec()` internals.
    """
    count = session.scalar(select(func.count()).select_from(Case))
    return {"count": count if count is not None else 0}
