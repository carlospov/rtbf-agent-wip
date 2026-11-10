"""Kanban board route (issue #54): the browsable `Case` list, grouped by `CaseState`.

Renders `templates/kanban.html` via `fastapi.templating.Jinja2Templates`, per `api/app.py`'s
module docstring's decision to use server-rendered HTML rather than an SPA. This module is the
whole of the kanban board's server-side surface -- route, query, and i18n resolution -- kept
separate from `api/app.py`'s plumbing routes (health check, `/cases/count`) so it can be reasoned
about (and AST-scanned for R23 compliance, see below) on its own.

**R23 -- every user-facing string on this page is sourced from `I18nProvider.translate()`, never a
literal embedded here or in the template.** Concretely:

- This module never passes a literal string as a *value* into a `dict` literal that ends up in the
  Jinja2 rendering context -- every piece of display text (column headers, card field labels, the
  empty-state notice) is the result of `_i18n.translate(key, lang)`, where `key` itself is a
  lookup key (not shown to a human) and is therefore fine as a literal. This same invariant is
  checked mechanically by
  `tests/api/test_kanban.py::test_no_literal_user_facing_strings_in_bot_or_kanban_code` (this
  issue's kanban half of the shared test name from issue #14's Telegram bot, adapted for a
  server-rendered-template UI rather than a bot's `send_message`/`InlineButton` calls -- see that
  test module's docstring for why the check shape differs).
- `templates/kanban.html` itself contains no literal English text outside `{{ ... }}`
  interpolations and `{% ... %}` control structures (also checked by the same test, via a
  companion scan over the template file) -- it only ever displays values this module already
  translated and put in the context, never hardcodes a label or notice of its own.

**Interface language resolution:** reuses `rtbf_agent.config.resolve_interface_language()`
verbatim -- the exact same single-process-wide `INTERFACE_LANGUAGE` mechanism issue #14's Telegram
bot uses -- rather than inventing a second one (spec's requirement that interface language is
configured once, independently of any per-case `drafting_language`/`applicable_jurisdiction`,
R22).

**Grouping order:** cases are bucketed into `CASE_STATES_IN_ORDER` (`core/models.py`), the same
10-state kanban order the spec defines -- never re-derived or re-sorted here.

**Linking to case detail:** each card links to `/cases/{id}`, served by `rtbf_agent.api.case_detail`
(issue #55) -- the case detail/event audit trail view. That route lives in its own module, not
here (out of scope for this one, see issue #55's "Not in scope").
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from rtbf_agent.api.deps import get_session
from rtbf_agent.config import resolve_interface_language
from rtbf_agent.core.interfaces import I18nProvider
from rtbf_agent.core.models import CASE_STATES_IN_ORDER, Case, CaseState
from rtbf_agent.plugins.i18n.resource import ResourceI18nProvider

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# One `I18nProvider` instance for the whole process, mirroring `deps.py`'s module-scoped engine
# caching rationale: this is a single self-hosted process, not a per-request concern.
_i18n: I18nProvider = ResourceI18nProvider()

# Maps each kanban column (== each `CaseState`) to its i18n lookup key -- never the column's
# display text itself, so `translate()` is the only place English (or any language's) text for a
# column header exists. Public (no leading underscore): `rtbf_agent.api.case_detail` (issue #55)
# imports this verbatim to render a case's own state label identically to how it appears as a
# column header here -- one mapping, not two independently-maintained copies that could drift.
CASE_STATE_LABEL_KEYS: dict[CaseState, str] = {
    CaseState.DISCOVERED: "kanban.column.discovered",
    CaseState.CLASSIFIED: "kanban.column.classified",
    CaseState.DRAFT_READY: "kanban.column.draft_ready",
    CaseState.AWAITING_HUMAN_APPROVAL: "kanban.column.awaiting_human_approval",
    CaseState.AWAITING_RESPONSE: "kanban.column.awaiting_response",
    CaseState.REQUIRES_HUMAN_ACTION: "kanban.column.requires_human_action",
    CaseState.DELETION_CONFIRMED: "kanban.column.deletion_confirmed",
    CaseState.REJECTED_ESCALATED: "kanban.column.rejected_escalated",
    CaseState.CLOSED: "kanban.column.closed",
}


@router.get("/cases")
def kanban_board(request: Request, session: Session = Depends(get_session)):
    """Render every `Case`, grouped into columns by `state` in `CASE_STATES_IN_ORDER`."""
    lang = resolve_interface_language()

    # Sorted in Python rather than `.order_by(Case.updated_at.desc())`: `ty` (this project's type
    # checker) doesn't resolve `.desc()` on `Case.updated_at`'s column descriptor the same way
    # SQLAlchemy's runtime does -- mirrors `cli.py`'s and `agent_loop.py`'s existing choice to sort/
    # filter in Python for the same class of typing mismatch.
    cases = sorted(session.exec(select(Case)).all(), key=lambda case: case.updated_at, reverse=True)
    cases_by_state: dict[CaseState, list[Case]] = {state: [] for state in CASE_STATES_IN_ORDER}
    for case in cases:
        cases_by_state[case.state].append(case)

    columns = []
    for state in CASE_STATES_IN_ORDER:
        # Computed once per column and reused for every card's "state" field below -- both are
        # the exact same key/lang, so translating it again per card would be redundant work with
        # no different result.
        label = _i18n.translate(CASE_STATE_LABEL_KEYS[state], lang)
        columns.append(
            {
                "label": label,
                "cases": [
                    {
                        "id": case.id,
                        "site": case.site,
                        "domain": case.domain,
                        "state": label,
                        "updated_at": case.updated_at.isoformat(),
                        "detail_url": f"/cases/{case.id}",
                    }
                    for case in cases_by_state[state]
                ],
            }
        )

    context = {
        "lang": lang,
        "page_title": _i18n.translate("kanban.page_title", lang),
        "columns": columns,
        "site_label": _i18n.translate("kanban.card.site_label", lang),
        "domain_label": _i18n.translate("kanban.card.domain_label", lang),
        "state_label": _i18n.translate("kanban.card.state_label", lang),
        "updated_label": _i18n.translate("kanban.card.updated_label", lang),
        "empty_state_text": _i18n.translate("kanban.empty_state", lang),
        # Add-a-case form (issue #125's kanban half): posts to api/add_case.py's route -- see that
        # module's own docstring for why it's a separate module rather than living here.
        "add_case_heading": _i18n.translate("kanban.add_case_heading", lang),
        "add_case_input_label": _i18n.translate("kanban.add_case_input_label", lang),
        "add_case_button_text": _i18n.translate("kanban.add_case_button", lang),
    }
    return templates.TemplateResponse(request, "kanban.html", context)
