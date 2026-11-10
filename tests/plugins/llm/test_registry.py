"""Tests for `rtbf_agent.plugins.llm.registry` — the data-only provider registry (R17, R21).

Covers: every registry entry has the required fields populated with the right shapes, and the
registry covers the minimum-supported-providers set from R17 (anthropic, openai, openrouter,
opencode) without requiring any adapter-shaped code per entry.
"""

from __future__ import annotations

import pytest

from rtbf_agent.plugins.llm.registry import PROVIDER_REGISTRY, ProviderPreset


def test_provider_registry_entries_have_required_fields() -> None:
    for key, preset in PROVIDER_REGISTRY.items():
        assert isinstance(preset, ProviderPreset)
        assert preset.name == key
        assert preset.protocol in ("anthropic", "openai")
        assert preset.base_url.startswith("https://")
        assert preset.env_var
        assert isinstance(preset.models, tuple)


def test_registry_covers_r17_minimum_providers() -> None:
    required = {"anthropic", "openai", "openrouter", "opencode"}
    assert required.issubset(PROVIDER_REGISTRY.keys())


def test_provider_registry_is_immutable() -> None:
    """`PROVIDER_REGISTRY` is data-only (R17, R21); no code path should be able to mutate the
    module-level constant at runtime."""
    with pytest.raises(TypeError):
        PROVIDER_REGISTRY["rogue"] = PROVIDER_REGISTRY["anthropic"]  # type: ignore[index]


def test_only_anthropic_uses_the_anthropic_protocol() -> None:
    # Everything else in the registry must be OpenAI-protocol-shaped (R17's design collapse:
    # one adapter per protocol, not per vendor).
    for key, preset in PROVIDER_REGISTRY.items():
        if key == "anthropic":
            assert preset.protocol == "anthropic"
        else:
            assert preset.protocol == "openai"
