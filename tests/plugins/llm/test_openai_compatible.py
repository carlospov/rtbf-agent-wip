"""Tests for `rtbf_agent.plugins.llm.openai_compatible.OpenAICompatibleAdapter` (R16-R19).

Same `httpx.MockTransport` approach as the Anthropic adapter tests — no real network call. This
one class is exercised as OpenAI, OpenRouter, and opencode's shared adapter would be at runtime
(only `base_url`/`api_key` differ), plus a no-api-key case for self-hosted endpoints (Ollama).
"""

from __future__ import annotations

import json

import httpx
import pytest

from rtbf_agent.core.interfaces import LLMMessage, LLMProvider, LLMRequest, LLMResponse
from rtbf_agent.plugins.llm.openai_compatible import OpenAICompatibleAdapter


def _make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_openai_compatible_adapter_implements_llmprovider_contract() -> None:
    adapter = OpenAICompatibleAdapter(base_url="https://api.openai.com/v1", api_key="sk-test")
    assert isinstance(adapter, LLMProvider)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.openai.com/v1",  # OpenAI
        "https://openrouter.ai/api/v1",  # OpenRouter
        "https://opencode.ai/zen/v1",  # opencode Zen
    ],
)
async def test_complete_translates_request_and_response_correctly(base_url: str) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "some-model",
                "choices": [{"message": {"role": "assistant", "content": "hello back"}}],
            },
        )

    adapter = OpenAICompatibleAdapter(
        base_url=base_url, api_key="sk-test", client=_make_client(handler)
    )
    request = LLMRequest(
        messages=(LLMMessage(role="user", content="hi"),),
        model="some-model",
        max_tokens=128,
        temperature=0.5,
    )
    response = await adapter.complete(request)

    assert isinstance(response, LLMResponse)
    assert response.content == "hello back"
    assert response.model == "some-model"
    assert captured["url"] == f"{base_url}/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer sk-test"
    assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["body"]["max_tokens"] == 128
    assert captured["body"]["temperature"] == 0.5


async def test_complete_omits_authorization_header_when_no_api_key_given() -> None:
    """Self-hosted endpoints (Ollama, LM Studio) commonly need no API key at all (R18)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200, json={"model": "llama3", "choices": [{"message": {"content": "ok"}}]}
        )

    adapter = OpenAICompatibleAdapter(
        base_url="http://localhost:11434/v1", api_key=None, client=_make_client(handler)
    )
    request = LLMRequest(messages=(LLMMessage(role="user", content="hi"),), model="llama3")
    await adapter.complete(request)

    assert "authorization" not in captured["headers"]


async def test_complete_only_sends_fields_present_on_the_request() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"model": "some-model", "choices": [{"message": {"content": "ok"}}]}
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://api.openai.com/v1", api_key="sk-test", client=_make_client(handler)
    )
    request = LLMRequest(
        messages=(LLMMessage(role="user", content="just this"),), model="some-model"
    )
    await adapter.complete(request)

    body = captured["body"]
    assert "temperature" not in body
    assert "max_tokens" not in body
    assert set(body.keys()) == {"model", "messages"}


async def test_complete_raises_on_http_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    adapter = OpenAICompatibleAdapter(
        base_url="https://api.openai.com/v1", api_key="bad-key", client=_make_client(handler)
    )
    request = LLMRequest(messages=(LLMMessage(role="user", content="hi"),), model="some-model")
    with pytest.raises(RuntimeError, match="401"):
        await adapter.complete(request)


@pytest.mark.parametrize("body", [{"model": "some-model"}, {"model": "some-model", "choices": []}])
async def test_complete_raises_valueerror_on_missing_or_empty_choices(body: dict) -> None:
    """A 200 response with no/empty `choices` (error-shaped or non-standard) must surface as an
    error, not silently degrade to an empty-content `LLMResponse` indistinguishable from a
    legitimately empty completion."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    adapter = OpenAICompatibleAdapter(
        base_url="https://api.openai.com/v1", api_key="sk-test", client=_make_client(handler)
    )
    request = LLMRequest(messages=(LLMMessage(role="user", content="hi"),), model="some-model")
    with pytest.raises(ValueError, match="no choices"):
        await adapter.complete(request)


async def test_complete_treats_null_content_as_empty_string_not_none() -> None:
    """A `"content": null` message (valid for tool-call-only assistant turns) must not leak a
    `None` past the `LLMResponse.content: str` contract."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "some-model",
                "choices": [{"message": {"content": None}}],
            },
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://api.openai.com/v1", api_key="sk-test", client=_make_client(handler)
    )
    request = LLMRequest(messages=(LLMMessage(role="user", content="hi"),), model="some-model")
    response = await adapter.complete(request)

    assert response.content == ""


async def test_complete_raises_runtimeerror_on_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    adapter = OpenAICompatibleAdapter(
        base_url="https://api.openai.com/v1", api_key="sk-test", client=_make_client(handler)
    )
    request = LLMRequest(messages=(LLMMessage(role="user", content="hi"),), model="some-model")
    with pytest.raises(RuntimeError, match="non-JSON"):
        await adapter.complete(request)
