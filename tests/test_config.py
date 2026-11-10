"""Tests for `rtbf_agent.config` — requester identity + per-case-default resolution (R18, R22).

Mirrors `tests/plugins/llm/test_config.py`'s structure: env vars first, then the local gitignored
config file, then `None` (there is no third fallback layer here — see `config.py`'s docstring for
why). `RTBF_AGENT_CONFIG_PATH` is monkeypatched to a per-test tmp path so tests never depend on
(or pollute) the developer's actual `~/.config`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtbf_agent import config as config_module

# Captured at import time, before the autouse fixture below ever monkeypatches
# `DEFAULT_CONFIG_PATH`, so this reflects what actually ships.
_REAL_DEFAULT_CONFIG_PATH = config_module.DEFAULT_CONFIG_PATH


@pytest.fixture(autouse=True)
def _isolated_config_path(tmp_path, monkeypatch):
    """Point the config file resolver at an empty tmp path unless a test overrides it, and strip
    any RTBF_REQUESTER_NAME/RTBF_DEFAULT_*/RTBF_AGENT_* env var that might leak in from the host."""
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    for key in list(config_module.os.environ):
        if (
            key == "RTBF_REQUESTER_NAME"
            or key.startswith("RTBF_DEFAULT_")
            or key.startswith("RTBF_AGENT_")
        ):
            monkeypatch.delenv(key, raising=False)
    yield tmp_path


def test_resolve_global_config_all_none_when_nothing_configured() -> None:
    resolved = config_module.resolve_global_config()
    assert resolved.requester_name is None
    assert resolved.default_applicable_jurisdiction is None
    assert resolved.default_drafting_language is None


def test_env_vars_resolve_all_three_fields(monkeypatch) -> None:
    monkeypatch.setenv("RTBF_REQUESTER_NAME", "Ada Lovelace")
    monkeypatch.setenv("RTBF_DEFAULT_APPLICABLE_JURISDICTION", "GDPR")
    monkeypatch.setenv("RTBF_DEFAULT_DRAFTING_LANGUAGE", "en")
    resolved = config_module.resolve_global_config()
    assert resolved.requester_name == "Ada Lovelace"
    assert resolved.default_applicable_jurisdiction == "GDPR"
    assert resolved.default_drafting_language == "en"


def test_config_file_fills_in_fields_env_vars_did_not_set(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[case_defaults]\n"
        'requester_name = "File Person"\n'
        'applicable_jurisdiction = "GDPR"\n'
        'drafting_language = "en"\n'
    )
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    resolved = config_module.resolve_global_config()
    assert resolved.requester_name == "File Person"
    assert resolved.default_applicable_jurisdiction == "GDPR"
    assert resolved.default_drafting_language == "en"


def test_env_vars_take_priority_over_config_file(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[case_defaults]\nrequester_name = "File Person"\n')
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("RTBF_REQUESTER_NAME", "Env Person")
    resolved = config_module.resolve_global_config()
    assert resolved.requester_name == "Env Person"


def test_empty_env_var_clears_value_rather_than_falling_back_to_config_file(
    monkeypatch, tmp_path
) -> None:
    """An operator setting `RTBF_REQUESTER_NAME=""` is explicitly clearing the value -- it must
    resolve to `None`, not silently fall through to whatever the config file has (the naive
    `os.environ.get(...) or _file_str(...)` pattern would treat "" as "not set" and do exactly
    that)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[case_defaults]\nrequester_name = "File Person"\n')
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("RTBF_REQUESTER_NAME", "")
    resolved = config_module.resolve_global_config()
    assert resolved.requester_name is None


def test_empty_string_in_config_file_resolves_to_none(monkeypatch, tmp_path) -> None:
    """An explicitly empty string in the config file (`requester_name = ""`) carries no more
    information than an absent key -- both must resolve to `None`, not an ambiguous `""`."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[case_defaults]\nrequester_name = ""\n')
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    resolved = config_module.resolve_global_config()
    assert resolved.requester_name is None


def test_whitespace_only_string_in_config_file_resolves_to_none(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[case_defaults]\nrequester_name = "   "\n')
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    resolved = config_module.resolve_global_config()
    assert resolved.requester_name is None


def test_config_file_value_is_stripped_of_surrounding_whitespace(monkeypatch, tmp_path) -> None:
    """A stray trailing space (e.g. `applicable_jurisdiction = "GDPR "`) must not silently fail
    template lookup in `_draft()` -- it should be stripped here, at resolution time, where the
    cause is obvious, not surface as a confusing LookupError two steps downstream."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[case_defaults]\napplicable_jurisdiction = "  GDPR  "\n')
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    resolved = config_module.resolve_global_config()
    assert resolved.default_applicable_jurisdiction == "GDPR"


def test_env_var_value_is_stripped_of_surrounding_whitespace(monkeypatch) -> None:
    monkeypatch.setenv("RTBF_REQUESTER_NAME", "  Ada Lovelace  ")
    resolved = config_module.resolve_global_config()
    assert resolved.requester_name == "Ada Lovelace"


def test_whitespace_only_env_var_clears_value(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[case_defaults]\nrequester_name = "File Person"\n')
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("RTBF_REQUESTER_NAME", "   ")
    resolved = config_module.resolve_global_config()
    assert resolved.requester_name is None


def test_config_path_pointing_to_a_directory_raises_valueerror(monkeypatch, tmp_path) -> None:
    """A misconfigured `RTBF_AGENT_CONFIG_PATH` pointing at a directory must fail loud, not
    silently resolve every global default to `None` (which then surfaces later as a confusing
    `_classify()` ValueError that looks like a code bug, not a configuration mistake)."""
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(tmp_path))
    with pytest.raises(ValueError, match="not a regular file"):
        config_module.resolve_global_config()


def test_case_defaults_section_not_a_table_raises_valueerror(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('case_defaults = "not-a-table"\n')
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    with pytest.raises(ValueError, match=r"\[case_defaults\] section"):
        config_module.resolve_global_config()


def test_non_string_field_in_config_file_raises_valueerror(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[case_defaults]\nrequester_name = 12345\n")
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    with pytest.raises(ValueError, match="requester_name"):
        config_module.resolve_global_config()


def test_invalid_toml_in_config_file_raises_valueerror_naming_the_path(
    monkeypatch, tmp_path
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("this is not [valid toml")
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    with pytest.raises(ValueError, match=str(config_path)):
        config_module.resolve_global_config()


def test_default_config_path_lives_outside_the_repository() -> None:
    # Assert against the value captured before the autouse fixture monkeypatched
    # `DEFAULT_CONFIG_PATH`, so this proves what ships, not source-code formatting.
    assert Path.home() / ".config" / "rtbf-agent" / "config.toml" == _REAL_DEFAULT_CONFIG_PATH


def test_config_path_override_expands_tilde(monkeypatch, tmp_path) -> None:
    """Many non-shell contexts that set env vars (systemd units, Docker `ENV`, CI) never expand
    `~` themselves -- a literal `~/...` override must still resolve to the real home directory,
    not be treated as a relative directory literally named `~`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    config_path.write_text('[case_defaults]\nrequester_name = "Tilde Person"\n')
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", "~/config.toml")

    resolved = config_module.resolve_global_config()

    assert resolved.requester_name == "Tilde Person"
