"""Tests for `rtbf_agent.plugins.followup.imap_config` — local-only IMAP credential resolution
(R14, issue #102).

Mirrors `tests/plugins/senders/test_config.py`'s isolation approach: `DEFAULT_CONFIG_PATH` is
monkeypatched to a per-test tmp path so tests never depend on (or pollute) the developer's actual
`~/.config`, and any `RTBF_IMAP_*`/`RTBF_AGENT_*` env vars that might leak in from the host are
stripped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtbf_agent.plugins.followup import imap_config as config_module

_REAL_DEFAULT_CONFIG_PATH = config_module.DEFAULT_CONFIG_PATH


@pytest.fixture(autouse=True)
def _isolated_config_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    for key in list(config_module.os.environ):
        if key.startswith("RTBF_IMAP_") or key.startswith("RTBF_AGENT_"):
            monkeypatch.delenv(key, raising=False)
    yield tmp_path


def test_resolve_imap_config_raises_without_any_fields() -> None:
    with pytest.raises(LookupError, match="host"):
        config_module.resolve_imap_config()


def test_resolve_imap_config_from_env_vars(monkeypatch) -> None:
    monkeypatch.setenv("RTBF_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("RTBF_IMAP_PORT", "993")
    monkeypatch.setenv("RTBF_IMAP_USERNAME", "carlos")
    monkeypatch.setenv("RTBF_IMAP_PASSWORD", "sk-test-password")

    resolved = config_module.resolve_imap_config()

    assert resolved.host == "imap.example.com"
    assert resolved.port == 993
    assert resolved.username == "carlos"
    assert resolved.password == "sk-test-password"


def test_resolve_imap_config_from_config_file(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[imap]\n"
        'host = "imap.file-configured.example"\n'
        "port = 1993\n"
        'username = "file-user"\n'
        'password = "file-password"\n'
    )
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))

    resolved = config_module.resolve_imap_config()

    assert resolved.host == "imap.file-configured.example"
    assert resolved.port == 1993
    assert resolved.username == "file-user"
    assert resolved.password == "file-password"


def test_env_vars_take_priority_over_config_file(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[imap]\nhost = "from-file.example"\nusername = "file-user"\npassword = "file-password"\n'
    )
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("RTBF_IMAP_HOST", "from-env.example")

    resolved = config_module.resolve_imap_config()

    assert resolved.host == "from-env.example"
    # username wasn't set via env, so it falls through to the config file value.
    assert resolved.username == "file-user"


def test_default_port_is_993_when_unset(monkeypatch) -> None:
    monkeypatch.setenv("RTBF_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("RTBF_IMAP_USERNAME", "carlos")
    monkeypatch.setenv("RTBF_IMAP_PASSWORD", "sk-test-password")

    resolved = config_module.resolve_imap_config()

    assert resolved.port == 993


def test_missing_username_raises_lookuperror(monkeypatch) -> None:
    monkeypatch.setenv("RTBF_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("RTBF_IMAP_PASSWORD", "sk-test-password")

    with pytest.raises(LookupError, match="username"):
        config_module.resolve_imap_config()


def test_missing_password_raises_lookuperror(monkeypatch) -> None:
    monkeypatch.setenv("RTBF_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("RTBF_IMAP_USERNAME", "carlos")

    with pytest.raises(LookupError, match="password"):
        config_module.resolve_imap_config()


def test_invalid_port_raises_valueerror(monkeypatch) -> None:
    monkeypatch.setenv("RTBF_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("RTBF_IMAP_USERNAME", "carlos")
    monkeypatch.setenv("RTBF_IMAP_PASSWORD", "sk-test-password")
    monkeypatch.setenv("RTBF_IMAP_PORT", "not-a-number")

    with pytest.raises(ValueError, match="port"):
        config_module.resolve_imap_config()


def test_bool_port_in_config_file_raises_valueerror(monkeypatch, tmp_path) -> None:
    """`bool` is an `int` subclass (`int(True) == 1`) -- a stray `port = true` in the TOML file
    must not silently resolve to port 1 with no indication why."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[imap]\nhost = "imap.example.com"\nusername = "carlos"\n'
        'password = "sk-test-password"\nport = true\n'
    )
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))

    with pytest.raises(ValueError, match="port"):
        config_module.resolve_imap_config()


def test_non_integer_float_port_in_config_file_raises_valueerror(monkeypatch, tmp_path) -> None:
    """A fractional TOML float (`port = 993.5`) must not silently truncate to 993 -- that's a real
    misconfiguration, not a value `int(...)` should quietly accept."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[imap]\nhost = "imap.example.com"\nusername = "carlos"\n'
        'password = "sk-test-password"\nport = 993.5\n'
    )
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))

    with pytest.raises(ValueError, match="port"):
        config_module.resolve_imap_config()


def test_integer_valued_float_port_in_config_file_is_accepted(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[imap]\nhost = "imap.example.com"\nusername = "carlos"\n'
        'password = "sk-test-password"\nport = 993.0\n'
    )
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))

    resolved = config_module.resolve_imap_config()

    assert resolved.port == 993


def test_invalid_toml_in_config_file_raises_valueerror_naming_the_path(
    monkeypatch, tmp_path
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("this is not [valid toml")
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))

    with pytest.raises(ValueError, match=str(config_path)):
        config_module.resolve_imap_config()


def test_imap_section_not_a_table_raises_valueerror(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('imap = "not-a-table"\n')
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))

    with pytest.raises(ValueError, match=r"\[imap\] section"):
        config_module.resolve_imap_config()


def test_non_string_field_in_config_file_raises_valueerror(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[imap]\nhost = 12345\nusername = "carlos"\npassword = "sk-test-password"\n'
    )
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))

    with pytest.raises(ValueError, match="host"):
        config_module.resolve_imap_config()


def test_password_is_not_shown_in_imapconfig_repr(monkeypatch) -> None:
    monkeypatch.setenv("RTBF_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("RTBF_IMAP_USERNAME", "carlos")
    monkeypatch.setenv("RTBF_IMAP_PASSWORD", "sk-should-never-appear")

    resolved = config_module.resolve_imap_config()

    assert "sk-should-never-appear" not in repr(resolved)


def test_default_config_path_matches_the_other_plugins_default() -> None:
    """IMAP config resolves against the same local file the other plugins use (one file, multiple
    `[section]`s), and that file lives outside the repository."""
    assert Path.home() / ".config" / "rtbf-agent" / "config.toml" == _REAL_DEFAULT_CONFIG_PATH


def test_config_path_override_expands_tilde(monkeypatch, tmp_path) -> None:
    """Many non-shell contexts that set env vars (systemd units, Docker `ENV`, CI) never expand `~`
    themselves -- a literal `~/...` override must still resolve to the real home directory, not be
    treated as a relative directory literally named `~`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[imap]\nhost = "imap.example.com"\nusername = "carlos"\npassword = "sk-test-password"\n'
    )
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", "~/config.toml")

    resolved = config_module.resolve_imap_config()

    assert resolved.host == "imap.example.com"
