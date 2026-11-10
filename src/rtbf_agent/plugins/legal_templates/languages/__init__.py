"""Registry of drafting-language-content modules, keyed by language code (R21, R24).

Each module under this package (e.g. `en.py`) holds exactly one drafting language's body-template
phrasing/grammar/tone -- nothing about a specific jurisdiction's legal basis or citation. Adding a
new drafting language is a two-line change here (import the new module, add one registry entry)
plus one new module; it never requires touching `plugins/legal_templates/jurisdictions/` or
`provider.py`'s composition logic, proving the two axes are independent (R24).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from . import en


@dataclass(frozen=True)
class LanguageContent:
    """One drafting language's body-template content -- no legal basis/jurisdiction of its own."""

    code: str
    body_template: str
    """Format-string body template with a `{legal_basis}` placeholder (and other generic,
    jurisdiction-agnostic placeholders) filled in by a composing `LegalTemplateProvider` -- this
    template never names a specific jurisdiction, regulation, or article itself."""


LANGUAGES: MappingProxyType[str, LanguageContent] = MappingProxyType(
    {
        en.LANGUAGE_CODE: LanguageContent(
            code=en.LANGUAGE_CODE,
            body_template=en.BODY_TEMPLATE,
        ),
    }
)
"""Known drafting languages, keyed by language code. Not exhaustive by design (R21) -- a second
language is a new module under this package plus one new entry here."""
