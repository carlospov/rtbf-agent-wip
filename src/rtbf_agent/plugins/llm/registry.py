"""Provider registry for `LLMProvider` adapters — data only, no adapter logic (R17, R21).

Design precedent: `alibaba/open-code-review` (OCR) supports roughly fifteen LLM vendors behind a
single-method interface by recognizing that nearly every vendor speaks one of only two wire
protocols. Its `internal/llm/providers.go` is a flat `registry []Provider{...}` literal — each
entry `{Name, Protocol, BaseURL, AuthHeader, EnvVar, Models}`, no logic — so adding a vendor is a
data-only change as long as it speaks a protocol an existing client already implements. This
module copies that design shape (minus `AuthHeader`, which is determined implicitly by `protocol`
in each adapter -- `AnthropicAdapter` uses `x-api-key`, `OpenAICompatibleAdapter` uses
`Authorization: Bearer`): `PROVIDER_REGISTRY` below is our equivalent of OCR's
`registry []Provider{...}`, and adding a 5th, 6th, ... OpenAI-protocol vendor here never requires
touching `anthropic.py` or `openai_compatible.py` (see `docs/architecture/llm-provider-patterns.md`
for the full analysis this module implements).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

Protocol = Literal["anthropic", "openai"]
"""The only two wire protocols any adapter speaks (R17) — see `docs/architecture/llm-provider-
patterns.md` for why a third protocol shape is a design question, not a one-off adapter."""


@dataclass(frozen=True)
class ProviderPreset:
    """One known vendor's connection defaults — pure data, mirrors OCR's `Provider` struct.

    None of these fields are secrets: `env_var` *names* the environment variable a user sets
    locally to supply their own API key (R18); no key value is ever stored here.
    """

    name: str
    protocol: Protocol
    base_url: str
    env_var: str
    models: tuple[str, ...] = ()


PROVIDER_REGISTRY: MappingProxyType[str, ProviderPreset] = MappingProxyType(
    {
        "anthropic": ProviderPreset(
            name="anthropic",
            protocol="anthropic",
            base_url="https://api.anthropic.com",
            env_var="ANTHROPIC_API_KEY",
            models=(
                "claude-opus-4-5",
                "claude-sonnet-4-5",
                "claude-haiku-4-5",
            ),
        ),
        "openai": ProviderPreset(
            name="openai",
            protocol="openai",
            base_url="https://api.openai.com/v1",
            env_var="OPENAI_API_KEY",
            models=("gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini"),
        ),
        "openrouter": ProviderPreset(
            name="openrouter",
            protocol="openai",
            base_url="https://openrouter.ai/api/v1",
            env_var="OPENROUTER_API_KEY",
            models=(),  # OpenRouter's catalog is large and dynamic; not enumerated here.
        ),
        # opencode Zen exposes an OpenAI-compatible `/chat/completions` surface for its free-tier
        # models — verified live against `https://opencode.ai/zen/v1` on 2026-07-12, see
        # `docs/ci/code-review-setup.md`. Its Anthropic-shaped `/v1/messages` path (Claude models on
        # Zen) is *not* verified live; if a user picks one of those models, they should set
        # `protocol = "anthropic"` and `base_url = "https://opencode.ai/zen/v1"` explicitly via the
        # local config file/env vars rather than relying on this preset.
        "opencode": ProviderPreset(
            name="opencode",
            protocol="openai",
            base_url="https://opencode.ai/zen/v1",
            env_var="OPENCODE_API_KEY",
            models=(
                "big-pickle",
                "deepseek-v4-flash-free",
                "mimo-v2.5-free",
                "hy3-free",
                "nemotron-3-ultra-free",
                "north-mini-code-free",
            ),
        ),
    }
)
"""Known-vendor presets, keyed by provider name. Not exhaustive by design (R21): any other
OpenAI-compatible endpoint — a self-hosted Ollama/LM Studio/vLLM instance, a private gateway —
needs no registry entry at all. A caller supplies `protocol="openai"` and its own `base_url`
directly to `OpenAICompatibleAdapter`, exactly as OCR's "custom provider" path needs zero new
client code for `localhost:11434` (R18)."""
