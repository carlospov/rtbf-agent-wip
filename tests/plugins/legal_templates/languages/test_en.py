"""Tests for `rtbf_agent.plugins.legal_templates.languages.en` -- phrasing content only.

Covers: the module exposes a language code and a body template with a `{legal_basis}` placeholder,
and holds nothing that looks like jurisdiction-specific legal content (no reference to
`jurisdictions/`, no hardcoded regulation name/article number).
"""

from __future__ import annotations

import ast
from pathlib import Path

from rtbf_agent.plugins.legal_templates.languages import en

EN_MODULE_PATH = Path(en.__file__)


def test_language_code_is_en() -> None:
    assert en.LANGUAGE_CODE == "en"


def test_body_template_has_legal_basis_placeholder() -> None:
    assert "{legal_basis}" in en.BODY_TEMPLATE


def test_body_template_names_no_specific_jurisdiction() -> None:
    """Phrasing content must never hardcode a specific regulation/jurisdiction name (R24)."""
    lowered = en.BODY_TEMPLATE.lower()
    for forbidden in ("gdpr", "ccpa", "article 17", "regulation (eu)"):
        assert forbidden not in lowered


def test_en_module_does_not_import_jurisdictions_package() -> None:
    """Language content must never reference the jurisdiction subpackage (R24 independence)."""
    tree = ast.parse(EN_MODULE_PATH.read_text(encoding="utf-8"), filename=str(EN_MODULE_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                assert "jurisdictions" not in node.module
            for alias in node.names:
                assert "jurisdictions" not in alias.name
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "jurisdictions" not in alias.name
