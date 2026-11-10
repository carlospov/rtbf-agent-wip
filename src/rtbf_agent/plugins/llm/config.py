"""Local-only config resolution for `LLMProvider` adapters (R18).

Design precedent: OCR's `internal/llm/resolver.go` resolves `{url, token, model}` via a
priority-ordered strategy chain — its own config file, then its own env vars, then Claude Code's
env vars (piggybacking on another tool's config), then shell rc files — first complete triple
wins. `docs/architecture/llm-provider-patterns.md` recommends we keep that *shape* (a short-
circuiting, priority-ordered chain of resolution strategies) but drop the two OCR steps that don't
apply here: we're not nested inside another CLI, and shell-rc-parsing is an extra attack surface
not worth it for a security-sensitive tool. What's left is a 2-step chain:

1. Environment variables (`RTBF_LLM_*`, plus a known provider's own `env_var` from
   `registry.PROVIDER_REGISTRY` for the API key specifically).
2. A local, gitignored TOML config file (`~/.config/rtbf-agent/config.toml` by default, override
   with `RTBF_AGENT_CONFIG_PATH`) — never a file inside this repository.

No shell-rc parsing, no piggybacking on another tool's config. Both strategies must resolve fully
offline when `base_url` points at `localhost`/`127.0.0.1` — this module never assumes network
access to check anything. A field left unset by both strategies falls back to
`registry.PROVIDER_REGISTRY`'s static preset for the given provider, if one exists (never a
hardcoded endpoint outside the registry).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rtbf_agent.core.interfaces import LLMProvider
from rtbf_agent.plugins.llm.anthropic import AnthropicAdapter
from rtbf_agent.plugins.llm.openai_compatible import OpenAICompatibleAdapter
from rtbf_agent.plugins.llm.registry import PROVIDER_REGISTRY

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "rtbf-agent" / "config.toml"
"""Default local config file location. Never inside this repository; also listed in `.gitignore`
patterns for anyone who points `RTBF_AGENT_CONFIG_PATH` at a repo-local path instead."""


@dataclass(frozen=True)
class LLMConfig:
    """A fully-resolved `{protocol, base_url, api_key, model}` for one provider (R18)."""

    provider: str
    protocol: str
    base_url: str
    api_key: str | None
    model: str


def _config_file_path() -> Path:
    override = os.environ.get("RTBF_AGENT_CONFIG_PATH")
    # `.expanduser()`: many non-shell contexts that set env vars (systemd units, Docker `ENV`, CI)
    # never expand `~` themselves, so a literal `~/...` override would otherwise be treated as a
    # relative directory named `~`, silently fail to resolve, and every default would resolve to
    # missing with no indication why.
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def _load_config_file() -> dict[str, Any]:
    """Load the local TOML config file, or `{}` if it doesn't exist. Never touches the network."""
    path = _config_file_path()
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in config file {path}: {exc}") from exc


def resolve_llm_config(provider: str | None = None) -> LLMConfig:
    """Resolve one provider's config from env vars, then the local config file — nothing else.

    `provider` selects which `registry.PROVIDER_REGISTRY` preset to resolve against (or any other
    name for a self-hosted/custom endpoint that isn't in the registry, e.g. `"ollama"`); defaults
    to `RTBF_LLM_PROVIDER` if not given explicitly. Each field independently prefers the env var,
    falling back to the config file, then to the registry preset (`protocol`/`base_url`, and the
    first entry of `models` as a default `model` — API keys are never stored in the registry, so
    `api_key` only ever comes from an env var or the config file). Raises `LookupError` if a
    provider isn't given by
    either the argument or the env var, or if resolution still leaves `protocol`/`base_url`/`model`
    incomplete after both strategies. Raises `ValueError` if the resolved protocol isn't one of
    the two this project supports.
    """
    provider = provider or os.environ.get("RTBF_LLM_PROVIDER")
    if not provider:
        raise LookupError(
            "no LLM provider selected: pass provider= explicitly or set RTBF_LLM_PROVIDER"
        )

    llm_section = _load_config_file().get("llm", {})
    if not isinstance(llm_section, dict):
        raise ValueError(
            f"expected [llm] section to be a table in {_config_file_path()}, "
            f"got {type(llm_section).__name__}"
        )
    file_config: dict[str, Any] = llm_section.get(provider, {})
    if not isinstance(file_config, dict):
        raise ValueError(
            f"expected [llm.{provider}] section to be a table in {_config_file_path()}, "
            f"got {type(file_config).__name__}"
        )
    preset = PROVIDER_REGISTRY.get(provider)

    def _file_str(field_name: str) -> str | None:
        value = file_config.get(field_name)
        if value is None or isinstance(value, str):
            return value
        raise ValueError(
            f"expected {field_name!r} to be a string in [llm.{provider}] "
            f"({_config_file_path()}), got {type(value).__name__}"
        )

    protocol = (
        os.environ.get("RTBF_LLM_PROTOCOL")
        or _file_str("protocol")
        or (preset.protocol if preset else None)
    )
    base_url = (
        os.environ.get("RTBF_LLM_BASE_URL")
        or _file_str("base_url")
        or (preset.base_url if preset else None)
    )
    model = (
        os.environ.get("RTBF_LLM_MODEL")
        or _file_str("model")
        or (preset.models[0] if preset and preset.models else None)
    )
    api_key = (
        os.environ.get("RTBF_LLM_API_KEY")
        or (os.environ.get(preset.env_var) if preset else None)
        or _file_str("api_key")
    )

    missing = [
        field_name
        for field_name, value in (("protocol", protocol), ("base_url", base_url), ("model", model))
        if not value
    ]
    if missing:
        raise LookupError(
            f"incomplete LLM config for provider {provider!r}: missing {', '.join(missing)} "
            f"(set RTBF_LLM_* env vars or add [llm.{provider}] to {_config_file_path()})"
        )
    if protocol not in ("anthropic", "openai"):
        raise ValueError(
            f"unsupported LLM protocol {protocol!r} for provider {provider!r} "
            "(only 'anthropic' and 'openai' are implemented — see "
            "docs/architecture/llm-provider-patterns.md)"
        )
    # `missing` being empty proves protocol/base_url/model are all non-empty strings at this
    # point; the `assert`s below just make that provable to the type checker too.
    assert isinstance(protocol, str)
    assert isinstance(base_url, str)
    assert isinstance(model, str)

    return LLMConfig(
        provider=provider, protocol=protocol, base_url=base_url, api_key=api_key, model=model
    )


def build_llm_provider(provider: str | None = None) -> LLMProvider:
    """Resolve a real `LLMProvider` via `resolve_llm_config()` + the matching protocol adapter.

    Only two protocols exist (R17): `AnthropicAdapter` for `protocol == "anthropic"`,
    `OpenAICompatibleAdapter` for `protocol == "openai"` -- `resolve_llm_config()` already rejects
    any other value, so no `else` branch is reachable here in practice, but a defensive
    `ValueError` is raised anyway rather than silently falling through.

    Factored out of `cli.py` (where this used to be a private, CLI-only helper) so `api/document.py`
    -- which also needs a real `LLMProvider` to compose an additional-info draft on demand, not just
    the CLI/recurring-loop paths -- can build one the same way, rather than duplicating this
    protocol-selection logic a second time (R16: still exactly one place that ever chooses between
    the two concrete adapters).
    """
    llm_config = resolve_llm_config(provider)
    if llm_config.protocol == "anthropic":
        if llm_config.api_key is None:
            raise LookupError(f"no API key resolved for LLM provider {llm_config.provider!r}")
        return AnthropicAdapter(api_key=llm_config.api_key, base_url=llm_config.base_url)
    if llm_config.protocol == "openai":
        return OpenAICompatibleAdapter(base_url=llm_config.base_url, api_key=llm_config.api_key)
    raise ValueError(f"unsupported LLM protocol {llm_config.protocol!r}")
