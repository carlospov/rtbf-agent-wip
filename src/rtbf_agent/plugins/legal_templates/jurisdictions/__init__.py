"""Registry of jurisdiction-content modules, keyed by jurisdiction code (R21, R24).

Each module under this package (e.g. `gdpr.py`) holds exactly one jurisdiction's legal-basis
content -- nothing about phrasing, grammar, or a specific drafting language. Adding a new
jurisdiction is a two-line change here (import the new module, add one registry entry) plus one
new module; it never requires touching `plugins/legal_templates/languages/` or `provider.py`'s
composition logic, proving the two axes are independent (R24).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from . import gdpr


@dataclass(frozen=True)
class JurisdictionContent:
    """One jurisdiction's legal-basis content -- no phrasing, grammar, or language of its own."""

    code: str
    legal_basis: str
    """Citation/legal-basis text (e.g. a regulation name + article), independent of any drafting
    language -- a composing `LegalTemplateProvider` embeds this verbatim into whichever language's
    body template is requested."""


JURISDICTIONS: MappingProxyType[str, JurisdictionContent] = MappingProxyType(
    {
        gdpr.JURISDICTION_CODE: JurisdictionContent(
            code=gdpr.JURISDICTION_CODE,
            legal_basis=gdpr.LEGAL_BASIS,
        ),
    }
)
"""Known jurisdictions, keyed by jurisdiction code. Not exhaustive by design (R21) -- a second
jurisdiction is a new module under this package plus one new entry here."""
