"""Tests for the kanban board route (issue #54): `rtbf_agent.api.kanban` + `templates/kanban.html`.

Two concerns, mirrored from the issue's Verification section:

- `test_kanban_board_groups_cases_by_state_in_defined_order`: `Case`s render into columns in
  `CASE_STATES_IN_ORDER`, regardless of insertion/query order.
- `test_no_literal_user_facing_strings_in_bot_or_kanban_code`: no user-facing string on this page
  is a literal -- every one is sourced from `I18nProvider.translate()` (R23). This is this issue's
  kanban half of the naming convention `tests/plugins/notifiers/test_telegram.py`'s
  `test_no_literal_user_facing_strings_in_bot_code` established for the Telegram bot half (that
  file's own test used to share this "or_kanban" name too, back when it also defensively checked a
  not-yet-existing kanban module; the automated-case-loop rework's Phase 2 test rewrite dropped
  that now-redundant check, so its name was corrected then -- this file's own check still covers
  genuinely kanban-specific code, hence keeping "kanban" in its own name); that module's checker
  (an AST walk flagging literal strings passed to
  `send_message`/`answer_callback_query`/`InlineButton(...)`) doesn't transfer directly to a
  server-rendered-template UI -- there's no single "send text" call site to watch. Instead, two
  checks together give the same guarantee for this kind of UI:

  1. `_route_literal_dict_value_violations`: an AST walk over `api/kanban.py` flagging any bare
     string literal used as a rendering-context value anywhere in a route function -- built via a
     `{...}` dict literal, a `dict(...)` call, `x["key"] = "literal"` subscript assignment, or a
     dict comprehension (all four checked explicitly, since a contributor reaching for any of them
     in good faith to add a hardcoded label must be caught the same way, not just the narrowest
     literal-syntax case). Every piece of display text this route hands to the template arrives as
     one of these four shapes -- so if all of them are the result of `_i18n.translate(...)` (a
     `Call` node) or a DB attribute/computed value, never a bare `ast.Constant` string, then
     nothing hardcoded reaches the template. This deliberately does *not* flag literal strings
     elsewhere in the module (route paths in `@router.get(...)`, the template filename in
     `TemplateResponse(...)`, or the `key` argument to `translate(...)` itself) -- those are
     identifiers/paths, not text a human reads on the page.
  2. `_template_literal_text_violations`: a scan over `templates/kanban.html` in two passes --
     first extracting human-facing HTML attribute values (`alt`, `title`, `placeholder`,
     `aria-label`, `aria-valuetext`) and checking each for literal text (a whole-tag strip would
     otherwise discard an attribute's hardcoded text along with the tag, e.g.
     `<img alt="Deletion confirmed">`, even though a screen reader/tooltip renders that text to a
     user just as much as visible body copy); then stripping `<script>`/`<style>` blocks, Jinja
     `{{ ... }}`/`{% ... %}`/`{# ... #}` constructs, and remaining HTML tags, asserting whatever
     text remains is pure whitespace. If someone hardcodes a label or notice directly in the
     template (bypassing the route entirely, in the body or in an attribute), this catches it even
     though the AST check above never sees template files at all.

  Neither check is vacuous: for each of the shapes above, a `test_..._catches_...` test proves the
  check actually flags a planted violation of that specific shape, not just passes by construction
  (mirrors the self-check pattern already used by `tests/plugins/notifiers/test_telegram.py` and
  `tests/test_module_test_coverage.py`).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from rtbf_agent.api.app import app
from rtbf_agent.api.deps import get_session
from rtbf_agent.core.models import CASE_STATES_IN_ORDER, Case, CaseState
from rtbf_agent.plugins.i18n.resource import ResourceI18nProvider

KANBAN_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "rtbf_agent" / "api" / "kanban.py"
)
KANBAN_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "rtbf_agent" / "api" / "templates" / "kanban.html"
)


@pytest.fixture
def session() -> Generator[Session, None, None]:
    # StaticPool + check_same_thread=False: TestClient runs sync routes in a worker thread, a
    # different thread than this fixture -- see tests/api/test_app.py's identical fixture.
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


# --------------------------------------------------------------------------------------------- #
# Grouping order.
# --------------------------------------------------------------------------------------------- #


def test_kanban_board_groups_cases_by_state_in_defined_order(
    client: TestClient, session: Session
) -> None:
    # Deliberately inserted out of kanban order, and out of alphabetical order, so a passing test
    # can't be explained by insertion order or a coincidental sort.
    out_of_order_states = [
        CaseState.CLOSED,
        CaseState.DISCOVERED,
        CaseState.AWAITING_RESPONSE,
        CaseState.AWAITING_HUMAN_APPROVAL,
    ]
    for i, state in enumerate(out_of_order_states):
        session.add(
            Case(
                site=f"Site {i}",
                domain=f"site{i}.example.com",
                target_email="me@example.com",
                state=state,
            )
        )
    session.commit()

    response = client.get("/cases")
    assert response.status_code == 200
    body = response.text

    i18n = ResourceI18nProvider()
    labels_in_order = [i18n.translate(_column_key(state), "en") for state in CASE_STATES_IN_ORDER]

    # Every column header must be present...
    positions = []
    for label in labels_in_order:
        idx = body.find(f">{label}<")
        assert idx != -1, f"column header {label!r} not found in rendered board"
        positions.append(idx)

    # ...and appear in exactly `CASE_STATES_IN_ORDER`'s order, not insertion order or alphabetical.
    assert positions == sorted(positions), (
        "kanban column headers are not rendered in CASE_STATES_IN_ORDER's defined order"
    )

    # Each inserted case's site name must appear after its own column's header and before the next
    # column's header -- i.e. actually grouped under the right state, not just listed anywhere.
    for i, state in enumerate(out_of_order_states):
        state_index = CASE_STATES_IN_ORDER.index(state)
        column_start = positions[state_index]
        column_end = positions[state_index + 1] if state_index + 1 < len(positions) else len(body)
        site_idx = body.find(f"Site {i}", column_start, column_end)
        assert site_idx != -1, (
            f"Case in state {state!r} (Site {i}) not rendered within its own column"
        )


def test_kanban_board_sorts_cases_within_a_column_by_updated_at_descending(
    client: TestClient, session: Session
) -> None:
    """Cases in the same column render most-recently-updated first -- the sort order
    `kanban_board()` documents as deliberate, not just an incidental side effect of query order."""
    from datetime import UTC, datetime

    older = Case(
        site="Older Site",
        domain="older.example.com",
        target_email="me@example.com",
        state=CaseState.DISCOVERED,
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    newer = Case(
        site="Newer Site",
        domain="newer.example.com",
        target_email="me@example.com",
        state=CaseState.DISCOVERED,
        updated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    # Inserted oldest-first, so a passing test can't be explained by insertion/query order.
    session.add(older)
    session.add(newer)
    session.commit()

    response = client.get("/cases")
    body = response.text

    newer_idx = body.find("Newer Site")
    older_idx = body.find("Older Site")
    assert newer_idx != -1
    assert older_idx != -1
    assert newer_idx < older_idx, "most-recently-updated case must render first within its column"


def _column_key(state: CaseState) -> str:
    """Mirror of `api/kanban.py`'s `CASE_STATE_LABEL_KEYS` mapping, kept independent so this test
    doesn't just re-import (and therefore trivially agree with) the module under test."""
    return {
        CaseState.DISCOVERED: "kanban.column.discovered",
        CaseState.CLASSIFIED: "kanban.column.classified",
        CaseState.DRAFT_READY: "kanban.column.draft_ready",
        CaseState.AWAITING_HUMAN_APPROVAL: "kanban.column.awaiting_human_approval",
        CaseState.AWAITING_RESPONSE: "kanban.column.awaiting_response",
        CaseState.REQUIRES_HUMAN_ACTION: "kanban.column.requires_human_action",
        CaseState.DELETION_CONFIRMED: "kanban.column.deletion_confirmed",
        CaseState.REJECTED_ESCALATED: "kanban.column.rejected_escalated",
        CaseState.CLOSED: "kanban.column.closed",
    }[state]


def test_kanban_board_shows_empty_state_notice_for_states_with_no_cases(
    client: TestClient,
) -> None:
    i18n = ResourceI18nProvider()
    empty_text = i18n.translate("kanban.empty_state", "en")

    response = client.get("/cases")

    assert response.status_code == 200
    # With no cases at all, every one of the 10 columns shows the empty-state notice.
    assert response.text.count(empty_text) == len(CASE_STATES_IN_ORDER)


def test_root_redirects_to_cases(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/cases"


# --------------------------------------------------------------------------------------------- #
# No-literal-user-facing-strings (R23), kanban half.
# --------------------------------------------------------------------------------------------- #


def _is_literal_str(node: ast.expr) -> str | None:
    """Return the string value if `node` is a bare string constant, else `None`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _route_literal_dict_value_violations(module_path: Path) -> list[str]:
    """Every bare string literal used as a rendering-context value inside a route function.

    Every piece of text a route hands to the Jinja2 rendering context arrives as a dict value
    (the top-level context dict, and the per-column/per-card dicts nested inside its `columns`
    list) -- built *inside* a route's function body. If every such value is the result of
    `_i18n.translate(...)` or a DB attribute/computed expression -- never a bare string constant --
    then nothing hardcoded can reach the template through this route. Checks four ways a dict-like
    value could be built, since a future contributor (or LLM-assisted edit) reaching for any of
    them in good faith would otherwise slip past a check that only looked for `{...}` literal
    syntax:

    1. `{"key": "literal"}` -- an `ast.Dict` literal's `.values`.
    2. `dict(key="literal")` -- an `ast.Call` to the `dict` builtin, checking `.keywords`.
    3. `x["key"] = "literal"` -- an `ast.Assign`/`ast.AnnAssign` whose target is an `ast.Subscript`.
    4. `{k: "literal" for k in ...}` -- an `ast.DictComp`'s `.value` expression.

    Dict *keys*, route-decorator arguments, and the template filename argument to
    `TemplateResponse(...)` are untouched by this check: none of those are text a human reads on
    the page.

    Deliberately scoped to `FunctionDef`/`AsyncFunctionDef` bodies, not the whole module: a
    module-level dict like `CASE_STATE_LABEL_KEYS` (state -> i18n *lookup key*, never display text)
    is exactly the kind of literal-string dict this check must not flag -- those values are never
    handed to a human, only to `translate()`. Scoping to function bodies excludes that table
    without needing a name-based allowlist that a real violation could dodge by renaming.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    violations: list[str] = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Dict):
                for value in node.values:
                    literal = _is_literal_str(value) if value is not None else None
                    if literal is not None:
                        violations.append(
                            f"{module_path}:{value.lineno}: literal string used as a dict "
                            f"value: {literal!r}"
                        )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "dict"
            ):
                for kw in node.keywords:
                    literal = _is_literal_str(kw.value)
                    if literal is not None:
                        violations.append(
                            f"{module_path}:{kw.value.lineno}: literal string used as a "
                            f"dict(...) keyword value: {literal!r}"
                        )
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Subscript):
                literal = _is_literal_str(node.value)
                if literal is not None:
                    violations.append(
                        f"{module_path}:{node.value.lineno}: literal string assigned via "
                        f"subscript: {literal!r}"
                    )
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Subscript)
                and node.value is not None
            ):
                literal = _is_literal_str(node.value)
                if literal is not None:
                    violations.append(
                        f"{module_path}:{node.value.lineno}: literal string assigned via "
                        f"annotated subscript: {literal!r}"
                    )
            elif isinstance(node, ast.DictComp):
                literal = _is_literal_str(node.value)
                if literal is not None:
                    violations.append(
                        f"{module_path}:{node.value.lineno}: literal string used as a dict "
                        f"comprehension value: {literal!r}"
                    )
    return violations


_TEXTUAL_ATTRS = ("alt", "title", "placeholder", "aria-label", "aria-valuetext")


def _strip_jinja(text: str) -> str:
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)
    text = re.sub(r"\{\{.*?\}\}", "", text, flags=re.DOTALL)
    text = re.sub(r"\{%.*?%\}", "", text, flags=re.DOTALL)
    return text


def _template_literal_text_violations(template_path: Path) -> list[str]:
    """Any non-whitespace text in `template_path` outside Jinja constructs, tags, and CSS/JS.

    Two passes:

    1. Human-facing HTML *attribute* values (`alt`, `title`, `placeholder`, `aria-label`,
       `aria-valuetext`) are extracted and checked *before* tags are stripped -- a whole-tag strip
       (`<[^>]*>`) would otherwise discard an attribute's hardcoded text along with the tag itself,
       making e.g. `<img alt="Deletion confirmed">` invisible to this check even though a screen
       reader renders that text to a user just as much as visible body copy does.
    2. Strips, in order: `<script>...</script>` and `<style>...</style>` blocks (their contents
       are CSS/JS, not page copy); Jinja comments `{# ... #}`, expressions `{{ ... }}`, and
       statements `{% ... %}`; and every remaining HTML tag (`<...>`, including `<!DOCTYPE ...>`).
       Whatever text is left is what a browser would actually render as page copy -- for a
       template that sources all its copy from the rendering context, that must be nothing but
       whitespace between tags.
    """
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


def test_no_literal_user_facing_strings_in_bot_or_kanban_code() -> None:
    violations = _route_literal_dict_value_violations(
        KANBAN_MODULE_PATH
    ) + _template_literal_text_violations(KANBAN_TEMPLATE_PATH)
    assert not violations, "\n".join(violations)


def test_route_literal_dict_value_check_catches_a_real_violation(tmp_path: Path) -> None:
    """Prove `_route_literal_dict_value_violations` actually catches a hardcoded string, mirroring
    `tests/plugins/notifiers/test_telegram.py`'s own self-check pattern for its analogous check."""
    bad_module = tmp_path / "bad_kanban.py"
    bad_module.write_text(
        "def kanban_board():\n"
        '    context = {"page_title": "Cases", "empty_state_text": "No cases here"}\n'
        "    return context\n"
    )

    violations = _route_literal_dict_value_violations(bad_module)

    assert violations
    assert any("Cases" in v for v in violations)
    assert any("No cases here" in v for v in violations)


def test_route_literal_dict_value_check_catches_dict_call_syntax(tmp_path: Path) -> None:
    """`dict(key="literal")` is just as much a hardcoded value as a `{...}` literal -- a
    contributor reaching for either in good faith must be caught the same way."""
    bad_module = tmp_path / "bad_kanban.py"
    bad_module.write_text(
        "def kanban_board():\n"
        '    return dict(page_title="Cases", empty_state_text="No cases here")\n'
    )

    violations = _route_literal_dict_value_violations(bad_module)

    assert violations
    assert any("Cases" in v for v in violations)


def test_route_literal_dict_value_check_catches_subscript_assignment(tmp_path: Path) -> None:
    """`context["key"] = "literal"` builds the same rendering-context value a `{...}` literal
    would, just incrementally -- must be caught the same way."""
    bad_module = tmp_path / "bad_kanban.py"
    bad_module.write_text(
        "def kanban_board():\n"
        "    context = {}\n"
        '    context["page_title"] = "Cases"\n'
        "    return context\n"
    )

    violations = _route_literal_dict_value_violations(bad_module)

    assert violations
    assert any("Cases" in v for v in violations)


def test_route_literal_dict_value_check_catches_annotated_subscript_assignment(
    tmp_path: Path,
) -> None:
    """`context["key"]: str = "literal"` is the annotated-assignment sibling of plain subscript
    assignment -- rare in practice, but the docstring documents both, so both must be caught."""
    bad_module = tmp_path / "bad_kanban.py"
    bad_module.write_text(
        "def kanban_board():\n"
        "    context = {}\n"
        '    context["page_title"]: str = "Cases"\n'
        "    return context\n"
    )

    violations = _route_literal_dict_value_violations(bad_module)

    assert violations
    assert any("Cases" in v for v in violations)


def test_route_literal_dict_value_check_catches_dict_comprehension(tmp_path: Path) -> None:
    """A dict comprehension whose value expression is a bare string constant is still a
    hardcoded value, even though it's built inside a comprehension rather than a plain literal."""
    bad_module = tmp_path / "bad_kanban.py"
    bad_module.write_text("def kanban_board():\n    return {k: 'Cases' for k in ['page_title']}\n")

    violations = _route_literal_dict_value_violations(bad_module)

    assert violations
    assert any("Cases" in v for v in violations)


def test_template_literal_text_check_catches_a_real_violation(tmp_path: Path) -> None:
    """Prove `_template_literal_text_violations` actually catches hardcoded template text."""
    bad_template = tmp_path / "bad_kanban.html"
    bad_template.write_text("<html><body><h1>Cases</h1><p>{{ empty_state_text }}</p></body></html>")

    violations = _template_literal_text_violations(bad_template)

    assert violations
    assert any("Cases" in v for v in violations)


def test_template_literal_text_check_ignores_style_and_jinja_constructs(tmp_path: Path) -> None:
    """The real template (all CSS + Jinja, no hardcoded copy) must pass -- proves the check isn't
    just trivially strict (e.g. flagging CSS property names) in a way that would force fake copy
    into `<style>` to appease it."""
    ok_template = tmp_path / "ok_kanban.html"
    ok_template.write_text(
        "<!DOCTYPE html>\n"
        "<html><head><style>body { color: red; }</style></head>\n"
        "<body>{# a comment #}{% for x in y %}<p>{{ x.label }}</p>{% endfor %}</body></html>\n"
    )

    violations = _template_literal_text_violations(ok_template)

    assert not violations


def test_template_literal_text_check_catches_hardcoded_attribute_values(tmp_path: Path) -> None:
    """A whole-tag strip (`<[^>]*>`) discards an attribute's hardcoded text along with the tag
    itself -- `<img alt="Deletion confirmed">` must not become invisible to this check just
    because the text lives in an attribute value rather than between tags. A screen reader or
    tooltip renders that text to a user exactly as much as visible body copy."""
    bad_template = tmp_path / "bad_kanban.html"
    bad_template.write_text(
        '<html><body><img alt="Deletion confirmed">'
        '<div title="Approve this case">{{ x }}</div></body></html>'
    )

    violations = _template_literal_text_violations(bad_template)

    assert violations
    assert any("Deletion confirmed" in v for v in violations)
    assert any("Approve this case" in v for v in violations)


def test_template_literal_text_check_allows_jinja_inside_attribute_values(tmp_path: Path) -> None:
    """An attribute value that's entirely a Jinja expression (no hardcoded text alongside it) is
    not a violation -- only literal text inside the attribute is."""
    ok_template = tmp_path / "ok_kanban.html"
    ok_template.write_text('<html><body><img alt="{{ x.label }}"></body></html>')

    violations = _template_literal_text_violations(ok_template)

    assert not violations
