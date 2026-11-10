"""Tests for `rtbf_agent.plugins.websearch.config` — local-only config resolution for SearXNG.

Covers the resolution chain: env vars first, then the local gitignored config file.
`RTBF_AGENT_CONFIG_PATH` is monkeypatched to a per-test tmp path so tests never depend on (or
pollute) the developer's actual `~/.config`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rtbf_agent.plugins.websearch import config as config_module

# Captured at import time, before the autouse fixture below ever monkeypatches
# `DEFAULT_CONFIG_PATH`, so this reflects what actually ships.
_REAL_DEFAULT_CONFIG_PATH = config_module.DEFAULT_CONFIG_PATH


@pytest.fixture(autouse=True)
def _isolated_config_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    for key in list(config_module.os.environ):
        if key.startswith("RTBF_SEARXNG_") or key.startswith("RTBF_AGENT_"):
            monkeypatch.delenv(key, raising=False)
    yield


def test_resolve_searxng_config_raises_without_a_base_url() -> None:
    with pytest.raises(LookupError, match="base_url"):
        config_module.resolve_searxng_config()


def test_resolve_searxng_config_from_env_vars(monkeypatch) -> None:
    monkeypatch.setenv("RTBF_SEARXNG_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("RTBF_SEARXNG_API_KEY", "test-token")
    resolved = config_module.resolve_searxng_config()
    assert resolved.base_url == "http://localhost:8080"
    assert resolved.api_key == "test-token"


def test_api_key_defaults_to_none(monkeypatch) -> None:
    monkeypatch.setenv("RTBF_SEARXNG_BASE_URL", "http://localhost:8080")
    resolved = config_module.resolve_searxng_config()
    assert resolved.api_key is None


def test_env_vars_take_priority_over_config_file(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[websearch]\nbase_url = "http://file-configured.example"\n')
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("RTBF_SEARXNG_BASE_URL", "http://env-configured.example")
    resolved = config_module.resolve_searxng_config()
    assert resolved.base_url == "http://env-configured.example"


def test_config_file_fills_in_base_url_and_api_key(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[websearch]\nbase_url = "http://localhost:8080"\napi_key = "file-token"\n'
    )
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    resolved = config_module.resolve_searxng_config()
    assert resolved.base_url == "http://localhost:8080"
    assert resolved.api_key == "file-token"


def test_invalid_toml_in_config_file_raises_valueerror_naming_the_path(
    monkeypatch, tmp_path
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("this is not [valid toml")
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    with pytest.raises(ValueError, match=re.escape(str(config_path))):
        config_module.resolve_searxng_config()


def test_websearch_section_not_a_table_raises_valueerror(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('websearch = "not-a-table"\n')
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    with pytest.raises(ValueError, match=r"\[websearch\] section"):
        config_module.resolve_searxng_config()


def test_non_string_field_in_config_file_raises_valueerror(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[websearch]\nbase_url = 12345\n")
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    with pytest.raises(ValueError, match="base_url"):
        config_module.resolve_searxng_config()


def test_default_config_path_lives_outside_the_repository() -> None:
    assert Path.home() / ".config" / "rtbf-agent" / "config.toml" == _REAL_DEFAULT_CONFIG_PATH


def test_config_path_override_expands_tilde(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    config_path.write_text('[websearch]\nbase_url = "http://tilde-configured.example"\n')
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", "~/config.toml")

    resolved = config_module.resolve_searxng_config()

    assert resolved.base_url == "http://tilde-configured.example"
