"""Tests for `rtbf_agent.plugins.llm.mock.MockLLMProvider` (R16, R17).

Covers:
- `MockLLMProvider` implements the `LLMProvider` contract (can be instantiated, `complete` is a
  coroutine returning an `LLMResponse`).
- Responses are deterministic (same request -> same response) and make no network call.
- The module imports no concrete LLM SDK, keeping it consistent with the "mock proves core never
  needs a real SDK" story even though this file lives under `plugins/`, not `core/`.
"""

from __future__ import annotations

import ast
from pathlib import Path

from rtbf_agent.core.interfaces import LLMMessage, LLMProvider, LLMRequest, LLMResponse
from rtbf_agent.plugins.llm.mock import MockLLMProvider

MOCK_MODULE_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "rtbf_agent" / "plugins" / "llm" / "mock.py"
)


def test_mockllmprovider_implements_llmprovider_contract() -> None:
    provider = MockLLMProvider()
    assert isinstance(provider, LLMProvider)


async def test_complete_returns_llmresponse() -> None:
    provider = MockLLMProvider()
    request = LLMRequest(
        messages=[LLMMessage(role="user", content="hello there")],
        model="mock-model-v1",
    )
    response = await provider.complete(request)
    assert isinstance(response, LLMResponse)
    assert response.model == "mock-model-v1"
    assert isinstance(response.content, str)
    assert response.content


async def test_complete_is_deterministic_for_the_same_request() -> None:
    provider = MockLLMProvider()
    request = LLMRequest(
        messages=[LLMMessage(role="user", content="what accounts exist for this email?")],
        model="mock-model-v1",
    )
    first = await provider.complete(request)
    second = await provider.complete(request)
    assert first.content == second.content
    assert first.model == second.model


async def test_complete_reflects_last_message_content() -> None:
    provider = MockLLMProvider()
    request = LLMRequest(
        messages=[
            LLMMessage(role="system", content="you are a classifier"),
            LLMMessage(role="user", content="classify this case"),
        ],
        model="mock-model-v1",
    )
    response = await provider.complete(request)
    assert "classify this case" in response.content


async def test_complete_handles_empty_messages() -> None:
    provider = MockLLMProvider()
    request = LLMRequest(messages=[], model="mock-model-v1")
    response = await provider.complete(request)
    assert isinstance(response, LLMResponse)
    assert response.model == "mock-model-v1"


async def test_complete_response_raw_defaults_to_empty_immutable_mapping() -> None:
    provider = MockLLMProvider()
    request = LLMRequest(messages=[LLMMessage(role="user", content="hi")], model="mock-model-v1")
    response = await provider.complete(request)
    assert dict(response.raw) == {}


def test_mock_provider_module_has_no_direct_llm_sdk_import() -> None:
    known_sdk_top_level_names = {
        "anthropic",
        "openai",
        "cohere",
        "mistralai",
        "google",
        "ollama",
        "groq",
        "together",
        "replicate",
        "boto3",
    }
    tree = ast.parse(MOCK_MODULE_PATH.read_text(encoding="utf-8"), filename=str(MOCK_MODULE_PATH))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in known_sdk_top_level_names:
                    offenders.append(f"{MOCK_MODULE_PATH}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".")[0]
            if top in known_sdk_top_level_names:
                offenders.append(f"{MOCK_MODULE_PATH}:{node.lineno}: from {node.module} import ...")
    assert not offenders, "mock.py imports a concrete LLM SDK directly:\n" + "\n".join(offenders)
