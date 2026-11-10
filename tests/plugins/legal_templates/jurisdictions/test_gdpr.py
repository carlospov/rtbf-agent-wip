"""Tests for `rtbf_agent.plugins.legal_templates.jurisdictions.gdpr` -- legal-basis content only.

Covers: the module exposes a jurisdiction code and legal-basis citation, and holds nothing that
looks like drafting-language phrasing (no `{`-style body-template placeholders, no reference to
`languages/`).
"""

from __future__ import annotations

import ast
from pathlib import Path

from rtbf_agent.plugins.legal_templates.jurisdictions import gdpr

GDPR_MODULE_PATH = Path(gdpr.__file__)


def test_jurisdiction_code_is_gdpr() -> None:
    assert gdpr.JURISDICTION_CODE == "GDPR"


def test_legal_basis_cites_article_17() -> None:
    assert "Article 17" in gdpr.LEGAL_BASIS
    assert "erasure" in gdpr.LEGAL_BASIS.lower()


def test_gdpr_module_does_not_import_languages_package() -> None:
    """Jurisdiction content must never reference the language subpackage (R24 independence)."""
    tree = ast.parse(GDPR_MODULE_PATH.read_text(encoding="utf-8"), filename=str(GDPR_MODULE_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                assert "languages" not in node.module
            for alias in node.names:
                assert "languages" not in alias.name
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "languages" not in alias.name
