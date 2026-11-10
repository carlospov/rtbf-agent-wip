"""Tests for `rtbf_agent.plugins.senders.config` — local-only SMTP credential resolution (R14).

Mirrors `tests/plugins/llm/test_config.py`'s isolation approach: `DEFAULT_CONFIG_PATH` is
monkeypatched to a per-test tmp path so tests never depend on (or pollute) the developer's actual
`~/.config`, and any `RTBF_SMTP_*`/`RTBF_AGENT_*` env vars that might leak in from the host are
stripped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtbf_agent.plugins.senders import config as config_module

_REAL_DEFAULT_CONFIG_PATH = config_module.DEFAULT_CONFIG_PATH


@pytest.fixture(autouse=True)
def _isolated_config_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    for key in list(config_module.os.environ):
        if key.startswith("RTBF_SMTP_") or key.startswith("RTBF_AGENT_"):
            monkeypatch.delenv(key, raising=False)
    yield tmp_path


def test_resolve_smtp_config_raises_without_host_or_from_address() -> None:
    with pytest.raises(LookupError, match="host"):
        config_module.resolve_smtp_config()


def test_resolve_smtp_config_from_env_vars(monkeypatch) -> None:
    monkeypatch.setenv("RTBF_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("RTBF_SMTP_PORT", "465")
    monkeypatch.setenv("RTBF_SMTP_USERNAME", "carlos")
    monkeypatch.setenv("RTBF_SMTP_PASSWORD", "sk-test-password")
    monkeypatch.setenv("RTBF_SMTP_FROM_ADDRESS", "carlos@example.com")
    monkeypatch.setenv("RTBF_SMTP_USE_TLS", "false")

    resolved = config_module.resolve_smtp_config()

    assert resolved.host == "smtp.example.com"
    assert resolved.port == 465
    assert resolved.username == "carlos"
    assert resolved.password == "sk-test-password"
    assert resolved.from_address == "carlos@example.com"
    assert resolved.use_tls is False


def test_resolve_smtp_config_from_config_file(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[smtp]\n"
        'host = "smtp.file-configured.example"\n'
        "port = 2525\n"
        'username = "file-user"\n'
        'password = "file-password"\n'
        'from_address = "file@example.com"\n'
    )
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))

    resolved = config_module.resolve_smtp_config()

    assert resolved.host == "smtp.file-configured.example"
    assert resolved.port == 2525
    assert resolved.username == "file-user"
    assert resolved.password == "file-password"
    assert resolved.from_address == "file@example.com"
    assert resolved.use_tls is True  # default when [smtp] doesn't set it


def test_env_vars_take_priority_over_config_file(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[smtp]\nhost = "from-file.example"\nfrom_address = "file@example.com"\n'
    )
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("RTBF_SMTP_HOST", "from-env.example")

    resolved = config_module.resolve_smtp_config()

    assert resolved.host == "from-env.example"
    # from_address wasn't set via env, so it falls through to the config file value.
    assert resolved.from_address == "file@example.com"


def test_username_without_password_raises_lookuperror(monkeypatch) -> None:
    monkeypatch.setenv("RTBF_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("RTBF_SMTP_FROM_ADDRESS", "carlos@example.com")
    monkeypatch.setenv("RTBF_SMTP_USERNAME", "carlos")

    with pytest.raises(LookupError, match="password"):
        config_module.resolve_smtp_config()


def test_default_port_is_587_when_unset(monkeypatch) -> None:
    monkeypatch.setenv("RTBF_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("RTBF_SMTP_FROM_ADDRESS", "carlos@example.com")

    resolved = config_module.resolve_smtp_config()

    assert resolved.port == 587


def test_invalid_port_raises_valueerror(monkeypatch) -> None:
    monkeypatch.setenv("RTBF_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("RTBF_SMTP_FROM_ADDRESS", "carlos@example.com")
    monkeypatch.setenv("RTBF_SMTP_PORT", "not-a-number")

    with pytest.raises(ValueError, match="port"):
        config_module.resolve_smtp_config()


def test_bool_port_in_config_file_raises_valueerror(monkeypatch, tmp_path) -> None:
    """`bool` is an `int` subclass (`int(True) == 1`) -- a stray `port = true` in the TOML file
    must not silently resolve to port 1 with no indication why."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[smtp]\nhost = "smtp.example.com"\nfrom_address = "carlos@example.com"\nport = true\n'
    )
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))

    with pytest.raises(ValueError, match="port"):
        config_module.resolve_smtp_config()


def test_non_integer_float_port_in_config_file_raises_valueerror(monkeypatch, tmp_path) -> None:
    """A fractional TOML float (`port = 587.5`) must not silently truncate to 587 -- that's a
    real misconfiguration, not a value `int(...)` should quietly accept."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[smtp]\nhost = "smtp.example.com"\nfrom_address = "carlos@example.com"\nport = 587.5\n'
    )
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))

    with pytest.raises(ValueError, match="port"):
        config_module.resolve_smtp_config()


def test_integer_valued_float_port_in_config_file_is_accepted(monkeypatch, tmp_path) -> None:
    """A whole-number TOML float (`port = 587.0`) is unambiguous -- unlike `587.5`, there's no
    silent precision loss, so this should resolve rather than raise."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[smtp]\nhost = "smtp.example.com"\nfrom_address = "carlos@example.com"\nport = 587.0\n'
    )
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))

    resolved = config_module.resolve_smtp_config()

    assert resolved.port == 587


def test_invalid_use_tls_raises_valueerror(monkeypatch) -> None:
    monkeypatch.setenv("RTBF_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("RTBF_SMTP_FROM_ADDRESS", "carlos@example.com")
    monkeypatch.setenv("RTBF_SMTP_USE_TLS", "maybe")

    with pytest.raises(ValueError, match="use_tls"):
        config_module.resolve_smtp_config()


def test_invalid_toml_in_config_file_raises_valueerror_naming_the_path(
    monkeypatch, tmp_path
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("this is not [valid toml")
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))

    with pytest.raises(ValueError, match=str(config_path)):
        config_module.resolve_smtp_config()


def test_smtp_section_not_a_table_raises_valueerror(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('smtp = "not-a-table"\n')
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))

    with pytest.raises(ValueError, match=r"\[smtp\] section"):
        config_module.resolve_smtp_config()


def test_non_string_field_in_config_file_raises_valueerror(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[smtp]\nhost = 12345\nfrom_address = "carlos@example.com"\n')
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))

    with pytest.raises(ValueError, match="host"):
        config_module.resolve_smtp_config()


def test_password_is_not_shown_in_smtpconfig_repr(monkeypatch) -> None:
    monkeypatch.setenv("RTBF_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("RTBF_SMTP_FROM_ADDRESS", "carlos@example.com")
    monkeypatch.setenv("RTBF_SMTP_PASSWORD", "sk-should-never-appear")
    monkeypatch.setenv("RTBF_SMTP_USERNAME", "carlos")

    resolved = config_module.resolve_smtp_config()

    assert "sk-should-never-appear" not in repr(resolved)


def test_default_config_path_matches_the_llm_config_default() -> None:
    """SMTP config resolves against the same local file the LLM config uses (one file, multiple
    `[section]`s), and that file lives outside the repository."""
    assert Path.home() / ".config" / "rtbf-agent" / "config.toml" == _REAL_DEFAULT_CONFIG_PATH


def test_config_path_override_expands_tilde(monkeypatch, tmp_path) -> None:
    """Many non-shell contexts that set env vars (systemd units, Docker `ENV`, CI) never expand
    `~` themselves -- a literal `~/...` override must still resolve to the real home directory,
    not be treated as a relative directory literally named `~`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[smtp]\nhost = "smtp.example.com"\nfrom_address = "carlos@example.com"\n'
    )
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", "~/config.toml")

    resolved = config_module.resolve_smtp_config()

    assert resolved.host == "smtp.example.com"
