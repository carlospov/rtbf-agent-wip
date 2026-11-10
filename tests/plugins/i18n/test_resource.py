"""Tests for `rtbf_agent.plugins.i18n.resource.ResourceI18nProvider`.

Verifies it correctly implements the `I18nProvider` contract from `core/interfaces.py`, resolves
the packaged `en` resource file, interpolates params, and -- per R23 -- fails loud on a missing
key or language rather than silently returning the key itself or an empty string.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtbf_agent.core.interfaces import I18nProvider
from rtbf_agent.plugins.i18n.resource import ResourceI18nProvider


def test_resourcei18nprovider_implements_contract() -> None:
    assert issubclass(ResourceI18nProvider, I18nProvider)


def test_resourcei18nprovider_is_concrete_class(tmp_path: Path) -> None:
    provider = ResourceI18nProvider(locales_dir=tmp_path)
    assert callable(provider.translate)


def test_translate_resolves_packaged_en_resource_with_params() -> None:
    """Deliberately uses the default (no `locales_dir`) constructor -- this is the one test in
    this module that must exercise the real packaged-resource path (`importlib.resources` against
    `plugins/i18n/locales/en.toml` as shipped), not a `tmp_path` stand-in, so that a packaging
    regression (e.g. the resource not being included in the built wheel) is actually caught."""
    provider = ResourceI18nProvider()

    result = provider.translate("greeting", "en", name="Carlos")

    assert result == "Hello, Carlos!"


def test_translate_resolves_dotted_key_with_multiple_params(tmp_path: Path) -> None:
    (tmp_path / "en.toml").write_text(
        '"case.status_notice" = "Case {case_id} is now {status}."\n', encoding="utf-8"
    )
    provider = ResourceI18nProvider(locales_dir=tmp_path)

    result = provider.translate("case.status_notice", "en", case_id="c-1", status="approved")

    assert result == "Case c-1 is now approved."


def test_missing_key_raises_rather_than_returning_the_key_itself(tmp_path: Path) -> None:
    (tmp_path / "en.toml").write_text('greeting = "Hello, {name}!"\n', encoding="utf-8")
    provider = ResourceI18nProvider(locales_dir=tmp_path)

    with pytest.raises(LookupError):
        provider.translate("this.key.does.not.exist", "en")


def test_missing_language_raises_rather_than_falling_back(tmp_path: Path) -> None:
    (tmp_path / "en.toml").write_text('greeting = "Hello, {name}!"\n', encoding="utf-8")
    provider = ResourceI18nProvider(locales_dir=tmp_path)

    with pytest.raises(LookupError):
        provider.translate("greeting", "xx")


@pytest.mark.parametrize(
    "lang",
    [
        "../../etc/passwd",
        "..",
        "en/../../etc",
        "en/",
        "a/b",
        "en.toml",
        "",
        "EN",
        "e",
    ],
)
def test_path_traversal_or_malformed_lang_is_rejected_not_resolved(
    lang: str, tmp_path: Path
) -> None:
    """`lang` is concatenated into a filename; anything but a strict language-code shape must be
    rejected before it ever reaches the filesystem (never traverse outside `locales_dir`).

    Asserts on the specific regex-rejection message rather than just `LookupError`: every one of
    these `lang` values fails to resolve to `secret.toml` regardless of whether the validation
    exists (`f"{lang}.toml"` never equals `"secret.toml"`), so a bare `LookupError` check can't
    tell "validation caught it" apart from "the file lookup happened to fail." Matching the
    regex-rejection message proves the validation layer -- not filesystem luck -- is what's
    blocking the input.
    """
    (tmp_path / "secret.toml").write_text('leak = "should never be reachable"\n', encoding="utf-8")
    provider = ResourceI18nProvider(locales_dir=tmp_path)

    with pytest.raises(LookupError, match="not a valid language code"):
        provider.translate("leak", lang)


def test_translate_against_custom_locales_dir(tmp_path: Path) -> None:
    (tmp_path / "fr.toml").write_text('bonjour = "Bonjour, {name}!"\n', encoding="utf-8")
    provider = ResourceI18nProvider(locales_dir=tmp_path)

    assert provider.translate("bonjour", "fr", name="Carlos") == "Bonjour, Carlos!"


def test_missing_key_in_custom_locales_dir_raises(tmp_path: Path) -> None:
    (tmp_path / "fr.toml").write_text('bonjour = "Bonjour!"\n', encoding="utf-8")
    provider = ResourceI18nProvider(locales_dir=tmp_path)

    with pytest.raises(LookupError):
        provider.translate("missing", "fr")


def test_missing_language_file_in_custom_locales_dir_raises(tmp_path: Path) -> None:
    provider = ResourceI18nProvider(locales_dir=tmp_path)

    with pytest.raises(LookupError):
        provider.translate("greeting", "de")


def test_non_string_value_in_resource_table_raises_value_error(tmp_path: Path) -> None:
    (tmp_path / "fr.toml").write_text("count = 3\n", encoding="utf-8")
    provider = ResourceI18nProvider(locales_dir=tmp_path)

    with pytest.raises(ValueError, match="flat key->string table"):
        provider.translate("count", "fr")


def test_invalid_toml_in_custom_locales_dir_raises_value_error(tmp_path: Path) -> None:
    (tmp_path / "fr.toml").write_text("not valid toml =====", encoding="utf-8")
    provider = ResourceI18nProvider(locales_dir=tmp_path)

    with pytest.raises(ValueError, match="invalid TOML"):
        provider.translate("anything", "fr")


def test_non_utf8_resource_file_raises_value_error_not_unhandled_traceback(
    tmp_path: Path,
) -> None:
    """A corrupt/binary resource file must be reported as a `ValueError`, not an unhandled
    `UnicodeDecodeError` -- `translate()`'s only documented failure mode is `LookupError`/
    `ValueError`."""
    (tmp_path / "fr.toml").write_bytes(b"bonjour = \xff\xfe invalid utf-8 \x80\x81")
    provider = ResourceI18nProvider(locales_dir=tmp_path)

    with pytest.raises(ValueError, match="invalid TOML"):
        provider.translate("bonjour", "fr")


def test_missing_format_param_raises_lookup_error_not_key_error(tmp_path: Path) -> None:
    """`str.format` raises `KeyError` for a missing named placeholder -- that must surface as
    `LookupError`, since `translate()`'s only documented failure mode is `LookupError`/`ValueError`,
    never a bare `KeyError` escaping from string interpolation internals."""
    (tmp_path / "fr.toml").write_text('bonjour = "Bonjour, {name}!"\n', encoding="utf-8")
    provider = ResourceI18nProvider(locales_dir=tmp_path)

    with pytest.raises(LookupError, match="error formatting translation"):
        provider.translate("bonjour", "fr")


def test_positional_placeholder_raises_lookup_error_not_index_error(tmp_path: Path) -> None:
    """A resource value with a positional `{0}`-style placeholder (no matching positional arg,
    since `translate()` only ever passes keyword `params`) must raise `IndexError` internally --
    which must surface as `LookupError`, per the same contract."""
    (tmp_path / "fr.toml").write_text('bonjour = "Bonjour, {0}!"\n', encoding="utf-8")
    provider = ResourceI18nProvider(locales_dir=tmp_path)

    with pytest.raises(LookupError, match="error formatting translation"):
        provider.translate("bonjour", "fr")


def test_malformed_format_spec_raises_value_error_not_lookup_error(tmp_path: Path) -> None:
    """A resource value with a bad format spec (e.g. `{name:d}` applied to a non-numeric value)
    raises `ValueError` from `str.format` itself. Unlike a missing param or positional placeholder
    (a caller-side issue, surfaced as `LookupError`), a malformed format spec means the resource
    file itself is broken -- so it surfaces as `ValueError`, consistent with the other
    resource-is-corrupted failures this module raises, per the `I18nProvider` interface contract
    documenting `ValueError` as a distinct failure mode from `LookupError`."""
    (tmp_path / "fr.toml").write_text('bonjour = "Bonjour, {name:d}!"\n', encoding="utf-8")
    provider = ResourceI18nProvider(locales_dir=tmp_path)

    with pytest.raises(ValueError, match="malformed format string"):
        provider.translate("bonjour", "fr", name="Carlos")
