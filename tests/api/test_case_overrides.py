"""Tests for the jurisdiction/drafting-language override routes (no-prompt-at-creation reversal):
`rtbf_agent.api.case_overrides`.

Mirrors `tests/api/test_draft.py`'s structure exactly (fixtures, R23 check helpers) -- see that
module's own docstring for why. The routes' happy-path/404/409/400 behavior is already covered
end-to-end in `tests/api/test_case_detail.py` (alongside the case-detail page's `can_edit_case_
overrides` rendering); this file's own job is the R23 no-literal-strings guarantee every other
`api/` module gets its own dedicated test for (OCR review finding on this PR: every other route
module -- `approval.py`/`draft.py`/`followup.py`/`recipient.py`/`document.py`/`vault.py` -- has
one, and `case_overrides.py` had been missed).
"""

from __future__ import annotations

import ast
from pathlib import Path

CASE_OVERRIDES_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "rtbf_agent" / "api" / "case_overrides.py"
)


def _string_literal_call_argument_violations(module_path: Path) -> list[str]:
    """Mirrors `tests/api/test_draft.py`'s identical helper -- see its own docstring."""
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


def test_no_literal_user_facing_strings_in_case_overrides_routes() -> None:
    assert _string_literal_call_argument_violations(CASE_OVERRIDES_MODULE_PATH) == []


def test_literal_string_violation_check_catches_a_real_keyword_violation(tmp_path: Path) -> None:
    bad_module = tmp_path / "bad_case_overrides.py"
    bad_module.write_text(
        "from fastapi import HTTPException\n"
        "def f():\n"
        '    raise HTTPException(status_code=404, detail="Case not found")\n'
    )
    violations = _string_literal_call_argument_violations(bad_module)
    assert violations


def test_literal_string_violation_check_catches_a_real_positional_violation(
    tmp_path: Path,
) -> None:
    bad_module = tmp_path / "bad_case_overrides_positional.py"
    bad_module.write_text(
        "from fastapi import HTTPException\n"
        "def f():\n"
        "    raise HTTPException(404, 'Case not found')\n"
    )
    assert _string_literal_call_argument_violations(bad_module)
