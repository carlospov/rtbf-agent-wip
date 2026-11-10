"""`RegistryLegalTemplateProvider` -- the composing `LegalTemplateProvider` (R22, R24).

This is the only place jurisdiction content (`jurisdictions/`) and drafting-language content
(`languages/`) meet. It holds no jurisdiction- or language-specific content itself -- only lookup
and composition logic -- so adding a new jurisdiction or a new drafting language never requires a
change here, only a new registry entry in the respective subpackage (R21).

Enforces:
- **R22** -- `jurisdiction` and `drafting_language` are resolved via two independent registries
  (`jurisdictions.JURISDICTIONS`, `languages.LANGUAGES`); neither lookup derives its key from the
  other.
- **R24** -- every `(jurisdiction, drafting_language)` combination of registered content is a
  valid lookup (see `test_all_combinations_of_interface_drafting_jurisdiction_are_valid`), proving
  the two axes are genuinely independent rather than a single combined key disguised as two
  parameters.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from rtbf_agent.core.interfaces import LegalTemplate, LegalTemplateProvider

from .jurisdictions import JURISDICTIONS, JurisdictionContent
from .languages import LANGUAGES, LanguageContent


class RegistryLegalTemplateProvider(LegalTemplateProvider):
    """Composes jurisdiction content and language content into a `LegalTemplate`.

    Accepts the two content registries as constructor arguments (defaulting to the real,
    module-level `JURISDICTIONS`/`LANGUAGES` registries) purely so tests can inject a throwaway
    stub registry -- e.g. to prove a new drafting language can be added without touching this
    class or any jurisdiction module -- without needing to mutate global, shared state.
    """

    def __init__(
        self,
        jurisdictions: Mapping[str, JurisdictionContent] = JURISDICTIONS,
        languages: Mapping[str, LanguageContent] = LANGUAGES,
    ) -> None:
        self._jurisdictions = jurisdictions
        self._languages = languages

    def get_template(self, jurisdiction: str, drafting_language: str) -> LegalTemplate:
        try:
            jurisdiction_content = self._jurisdictions[jurisdiction]
        except KeyError as exc:
            raise LookupError(f"no jurisdiction content registered for {jurisdiction!r}") from exc
        try:
            language_content = self._languages[drafting_language]
        except KeyError as exc:
            raise LookupError(
                f"no drafting-language content registered for {drafting_language!r}"
            ) from exc
        return LegalTemplate(
            jurisdiction=jurisdiction,
            drafting_language=drafting_language,
            legal_basis=jurisdiction_content.legal_basis,
            body_template=language_content.body_template,
        )

    def supported_jurisdictions(self) -> Sequence[str]:
        return tuple(self._jurisdictions)

    def supported_languages(self, jurisdiction: str) -> Sequence[str]:
        if jurisdiction not in self._jurisdictions:
            raise LookupError(f"no jurisdiction content registered for {jurisdiction!r}")
        # Every registered language applies to every registered jurisdiction -- the two axes are
        # independent (R24), so this never filters languages by jurisdiction.
        return tuple(self._languages)
