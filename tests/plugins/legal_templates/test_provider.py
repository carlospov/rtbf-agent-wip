"""Tests for `RegistryLegalTemplateProvider` -- proving jurisdiction/language independence (R22,
R24), per GitHub issue #15's verification checklist.

The three tests issue #15 names explicitly:
- `test_all_combinations_of_interface_drafting_jurisdiction_are_valid`
- `test_template_lookup_takes_jurisdiction_and_language_as_independent_params`
- `test_new_drafting_language_added_without_touching_existing_jurisdiction_templates`

Throwaway stub jurisdictions/languages (constructed inline, never as real modules under
`jurisdictions/`/`languages/`) are used where the issue explicitly sanctions it, to prove the
architecture generalizes beyond the one real jurisdiction (GDPR) and one real language (en) this
issue actually ships.
"""

from __future__ import annotations

import itertools

import pytest

from rtbf_agent.core.interfaces import LegalTemplate, LegalTemplateProvider
from rtbf_agent.plugins.legal_templates.jurisdictions import JURISDICTIONS, JurisdictionContent
from rtbf_agent.plugins.legal_templates.jurisdictions import gdpr as gdpr_module
from rtbf_agent.plugins.legal_templates.languages import LANGUAGES, LanguageContent
from rtbf_agent.plugins.legal_templates.provider import RegistryLegalTemplateProvider

# --------------------------------------------------------------------------------------------- #
# Basic contract compliance.
# --------------------------------------------------------------------------------------------- #


def test_provider_is_a_legal_template_provider() -> None:
    provider = RegistryLegalTemplateProvider()
    assert isinstance(provider, LegalTemplateProvider)


def test_gdpr_and_english_is_a_valid_real_combination() -> None:
    provider = RegistryLegalTemplateProvider()
    template = provider.get_template("GDPR", "en")
    assert isinstance(template, LegalTemplate)
    assert template.jurisdiction == "GDPR"
    assert template.drafting_language == "en"
    assert "Article 17" in template.legal_basis
    assert "{legal_basis}" in template.body_template


def test_unknown_jurisdiction_raises_lookup_error() -> None:
    provider = RegistryLegalTemplateProvider()
    with pytest.raises(LookupError):
        provider.get_template("NOT-A-REAL-JURISDICTION", "en")


def test_unknown_language_raises_lookup_error() -> None:
    provider = RegistryLegalTemplateProvider()
    with pytest.raises(LookupError):
        provider.get_template("GDPR", "not-a-real-language")


def test_supported_jurisdictions_and_languages_include_the_real_gdpr_en_pair() -> None:
    provider = RegistryLegalTemplateProvider()
    assert "GDPR" in provider.supported_jurisdictions()
    assert "en" in provider.supported_languages("GDPR")


def test_supported_languages_raises_lookup_error_for_unknown_jurisdiction() -> None:
    provider = RegistryLegalTemplateProvider()
    with pytest.raises(LookupError):
        provider.supported_languages("NOT-A-REAL-JURISDICTION")


# --------------------------------------------------------------------------------------------- #
# test_all_combinations_of_interface_drafting_jurisdiction_are_valid
# --------------------------------------------------------------------------------------------- #


def test_all_combinations_of_interface_drafting_jurisdiction_are_valid() -> None:
    """Every registered `(jurisdiction, language)` pair -- not just GDPR x en -- must resolve.

    Exercised against a provider seeded with one real jurisdiction (GDPR) plus one throwaway
    stub jurisdiction, and one real language (en) plus one throwaway stub language, so the test
    proves the *general* claim (every combination of every registered axis value is valid), not
    merely that the one real pair happens to work.
    """
    jurisdictions = {
        **JURISDICTIONS,
        "STUB-JURISDICTION": JurisdictionContent(
            code="STUB-JURISDICTION", legal_basis='Stub Act, Section 1 ("right to be forgotten")'
        ),
    }
    languages = {
        **LANGUAGES,
        "xx": LanguageContent(code="xx", body_template="Stub body {legal_basis} for {site}."),
    }
    provider = RegistryLegalTemplateProvider(jurisdictions=jurisdictions, languages=languages)

    combinations = list(itertools.product(provider.supported_jurisdictions(), ["en", "xx"]))
    expected = len(provider.supported_jurisdictions()) * 2  # N jurisdictions x 2 languages
    assert len(combinations) == expected

    for jurisdiction, language in combinations:
        template = provider.get_template(jurisdiction, language)
        assert template.jurisdiction == jurisdiction
        assert template.drafting_language == language
        assert template.legal_basis  # non-empty, sourced from the jurisdiction axis
        assert template.body_template  # non-empty, sourced from the language axis


# --------------------------------------------------------------------------------------------- #
# test_template_lookup_takes_jurisdiction_and_language_as_independent_params
# --------------------------------------------------------------------------------------------- #


def test_template_lookup_takes_jurisdiction_and_language_as_independent_params() -> None:
    """Varying one param alone changes only its half of the resulting template.

    Uses a throwaway second jurisdiction and second language (never touching the real GDPR/en
    modules) to show the lookup key is genuinely `(jurisdiction, language)` as two independent
    parameters, not one combined key disguised as two arguments -- e.g. a combined-key
    implementation might accidentally let `drafting_language` influence `legal_basis`, which this
    test would catch.
    """
    jurisdictions = {
        **JURISDICTIONS,
        "STUB-JURISDICTION": JurisdictionContent(
            code="STUB-JURISDICTION", legal_basis="Stub Act, Section 1"
        ),
    }
    languages = {
        **LANGUAGES,
        "xx": LanguageContent(code="xx", body_template="Stub body {legal_basis} for {site}."),
    }
    provider = RegistryLegalTemplateProvider(jurisdictions=jurisdictions, languages=languages)

    gdpr_en = provider.get_template("GDPR", "en")
    gdpr_xx = provider.get_template("GDPR", "xx")
    stub_en = provider.get_template("STUB-JURISDICTION", "en")

    # Same jurisdiction, different language: legal_basis is unchanged, body_template differs.
    assert gdpr_en.legal_basis == gdpr_xx.legal_basis
    assert gdpr_en.body_template != gdpr_xx.body_template

    # Same language, different jurisdiction: body_template is unchanged, legal_basis differs.
    assert gdpr_en.body_template == stub_en.body_template
    assert gdpr_en.legal_basis != stub_en.legal_basis


# --------------------------------------------------------------------------------------------- #
# test_new_drafting_language_added_without_touching_existing_jurisdiction_templates
# --------------------------------------------------------------------------------------------- #


def test_new_drafting_language_added_without_touching_existing_jurisdiction_templates() -> None:
    """Adding a throwaway second language never requires reading/modifying jurisdiction content.

    Snapshots the real GDPR jurisdiction content's identity (module attributes) before and after
    constructing a provider seeded with a *new* stub language, proving the GDPR module was never
    imported anew, mutated, or otherwise touched by the language addition -- only the language-side
    registry changed (here, injected via the constructor rather than editing
    `languages/__init__.py`, but the effect is the same: zero jurisdiction-side changes).
    """
    legal_basis_before = gdpr_module.LEGAL_BASIS
    jurisdiction_code_before = gdpr_module.JURISDICTION_CODE

    languages_with_new_stub = {
        **LANGUAGES,
        "stub-new-language": LanguageContent(
            code="stub-new-language", body_template="Brand new phrasing: {legal_basis}"
        ),
    }
    provider = RegistryLegalTemplateProvider(languages=languages_with_new_stub)

    # The new language works immediately against the untouched, real GDPR jurisdiction content.
    template = provider.get_template("GDPR", "stub-new-language")
    assert template.legal_basis == gdpr_module.LEGAL_BASIS
    assert template.body_template == "Brand new phrasing: {legal_basis}"

    # The existing GDPR jurisdiction module is byte-for-byte unchanged.
    assert legal_basis_before == gdpr_module.LEGAL_BASIS
    assert jurisdiction_code_before == gdpr_module.JURISDICTION_CODE

    # And the existing real (GDPR, en) combination still resolves exactly as before.
    unaffected = provider.get_template("GDPR", "en")
    assert unaffected.legal_basis == legal_basis_before
