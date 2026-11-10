"""Tests for `rtbf_agent.plugins.llm.config` — local-only config resolution (R18).

Covers the resolution chain: env vars first, then the local gitignored config file, then
`registry.PROVIDER_REGISTRY` defaults as the final fallback for any field neither of the first two
steps set. `RTBF_AGENT_CONFIG_PATH` is monkeypatched to a per-test tmp path so tests never depend
on (or pollute) the developer's actual `~/.config`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtbf_agent.plugins.llm import config as config_module
from rtbf_agent.plugins.llm.anthropic import AnthropicAdapter
from rtbf_agent.plugins.llm.openai_compatible import OpenAICompatibleAdapter

# Captured at import time, before the autouse fixture below ever monkeypatches
# `DEFAULT_CONFIG_PATH`, so this reflects what actually ships.
_REAL_DEFAULT_CONFIG_PATH = config_module.DEFAULT_CONFIG_PATH


@pytest.fixture(autouse=True)
def _isolated_config_path(tmp_path, monkeypatch):
    """Point the config file resolver at an empty tmp path unless a test overrides it, and strip
    any RTBF_LLM_*, RTBF_AGENT_* (including RTBF_AGENT_CONFIG_PATH itself), or *_API_KEY-shaped
    env vars that might leak in from the host."""
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    for key in list(config_module.os.environ):
        if key.startswith("RTBF_LLM_") or key.startswith("RTBF_AGENT_") or key.endswith("_API_KEY"):
            monkeypatch.delenv(key, raising=False)
    yield tmp_path


def test_resolve_llm_config_raises_without_a_provider() -> None:
    with pytest.raises(LookupError):
        config_module.resolve_llm_config()


def test_resolve_llm_config_uses_registry_defaults_and_env_api_key(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    resolved = config_module.resolve_llm_config("anthropic")
    assert resolved.protocol == "anthropic"
    assert resolved.base_url == "https://api.anthropic.com"
    assert resolved.api_key == "sk-test-123"
    assert resolved.model  # a registry default model is present


def test_env_vars_take_priority_over_registry_and_config_file(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[llm.anthropic]\nbase_url = "https://file-configured.example"\nmodel = "file-model"\n'
    )
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("RTBF_LLM_BASE_URL", "https://env-configured.example")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    resolved = config_module.resolve_llm_config("anthropic")
    assert resolved.base_url == "https://env-configured.example"
    # model wasn't set via env, so it falls through to the config file value.
    assert resolved.model == "file-model"


def test_config_file_fills_in_a_fully_custom_localhost_provider(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[llm.ollama]\n"
        'protocol = "openai"\n'
        'base_url = "http://localhost:11434/v1"\n'
        'model = "llama3"\n'
        'api_key = "not-needed"\n'
    )
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    resolved = config_module.resolve_llm_config("ollama")
    assert resolved.protocol == "openai"
    assert resolved.base_url == "http://localhost:11434/v1"
    assert resolved.model == "llama3"


def test_missing_fields_raise_lookuperror_naming_what_is_missing() -> None:
    with pytest.raises(LookupError, match="model"):
        config_module.resolve_llm_config("nonexistent-provider-with-no-registry-entry")


def test_unsupported_protocol_raises_valueerror(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[llm.weird]\nprotocol = "grpc"\nbase_url = "http://localhost:1234"\nmodel = "x"\n'
    )
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    with pytest.raises(ValueError, match="unsupported"):
        config_module.resolve_llm_config("weird")


def test_invalid_toml_in_config_file_raises_valueerror_naming_the_path(
    monkeypatch, tmp_path
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("this is not [valid toml")
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    with pytest.raises(ValueError, match=str(config_path)):
        config_module.resolve_llm_config("anthropic")


def test_llm_section_not_a_table_raises_valueerror(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm = "not-a-table"\n')
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    with pytest.raises(ValueError, match=r"\[llm\] section"):
        config_module.resolve_llm_config("anthropic")


def test_provider_section_not_a_table_raises_valueerror(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[llm]\nanthropic = "not-a-table"\n')
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    with pytest.raises(ValueError, match=r"\[llm\.anthropic\] section"):
        config_module.resolve_llm_config("anthropic")


def test_non_string_field_in_config_file_raises_valueerror(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[llm.anthropic]\nbase_url = 12345\nmodel = "x"\n')
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    with pytest.raises(ValueError, match="base_url"):
        config_module.resolve_llm_config("anthropic")


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
    config_path.write_text('[llm.anthropic]\nmodel = "tilde-model"\n')
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", "~/config.toml")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    resolved = config_module.resolve_llm_config("anthropic")

    assert resolved.model == "tilde-model"


# --------------------------------------------------------------------------------------------- #
# build_llm_provider() -- the protocol-selection factory extracted from cli.py's former private
# `_build_llm_provider()` (this PR), now shared with `api/document.py`'s draft route via
# `api/deps.py`'s `get_llm_provider()`. Previously only ever exercised indirectly, monkeypatched
# away entirely in `tests/test_cli.py` -- these tests are the first to exercise its actual
# anthropic/openai adapter-selection branching.
# --------------------------------------------------------------------------------------------- #


def test_build_llm_provider_returns_an_anthropic_adapter_for_the_anthropic_protocol(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    provider = config_module.build_llm_provider("anthropic")
    assert isinstance(provider, AnthropicAdapter)


def test_build_llm_provider_returns_an_openai_compatible_adapter_for_the_openai_protocol(
    monkeypatch, tmp_path
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[llm.custom]\nprotocol = "openai"\nbase_url = "http://localhost:11434/v1"\nmodel = "x"\n'
    )
    monkeypatch.setenv("RTBF_AGENT_CONFIG_PATH", str(config_path))
    provider = config_module.build_llm_provider("custom")
    assert isinstance(provider, OpenAICompatibleAdapter)


def test_build_llm_provider_raises_lookuperror_without_an_anthropic_api_key(monkeypatch) -> None:
    """R18: `resolve_llm_config()` itself allows `api_key` to resolve to `None` (a self-hosted
    OpenAI-compatible endpoint may need none) -- but `AnthropicAdapter` always needs one, so
    `build_llm_provider()` must refuse loud here rather than construct an adapter that would only
    fail later, deep inside its first real `.complete()` call."""
    with pytest.raises(LookupError, match="API key"):
        config_module.build_llm_provider("anthropic")
