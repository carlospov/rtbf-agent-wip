"""Tests for `rtbf_agent.plugins.llm.anthropic.AnthropicAdapter` (R16, R17, R19).

Uses `httpx.MockTransport` (no real network call, ever) to fake the Anthropic Messages API wire
shape and assert `AnthropicAdapter` translates `LLMRequest`/`LLMResponse` correctly and sends
nothing beyond what the request carries.
"""

from __future__ import annotations

import json

import httpx
import pytest

from rtbf_agent.core.interfaces import LLMMessage, LLMProvider, LLMRequest, LLMResponse
from rtbf_agent.plugins.llm.anthropic import AnthropicAdapter


def _make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_anthropic_adapter_implements_llmprovider_contract() -> None:
    adapter = AnthropicAdapter(api_key="sk-test")
    assert isinstance(adapter, LLMProvider)


async def test_complete_translates_request_and_response_correctly() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "hello back"}],
            },
        )

    adapter = AnthropicAdapter(
        api_key="sk-test", base_url="https://api.anthropic.com", client=_make_client(handler)
    )
    request = LLMRequest(
        messages=(
            LLMMessage(role="system", content="be terse"),
            LLMMessage(role="user", content="hi"),
        ),
        model="claude-sonnet-4-5",
        max_tokens=256,
        temperature=0.2,
    )
    response = await adapter.complete(request)

    assert isinstance(response, LLMResponse)
    assert response.content == "hello back"
    assert response.model == "claude-sonnet-4-5"
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-test"
    assert captured["headers"]["anthropic-version"]
    assert captured["body"]["system"] == "be terse"
    assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["body"]["max_tokens"] == 256
    assert captured["body"]["temperature"] == 0.2


async def test_complete_omits_optional_fields_not_in_request() -> None:
    """Optional fields (temperature, system) are omitted when not set on the `LLMRequest`.
    `max_tokens` is an exception: it's always sent because Anthropic's API requires it
    (defaults to 1024 per `AnthropicAdapter`'s documented R19 exception)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"model": "claude-sonnet-4-5", "content": [{"type": "text", "text": "ok"}]}
        )

    adapter = AnthropicAdapter(api_key="sk-test", client=_make_client(handler))
    request = LLMRequest(
        messages=(LLMMessage(role="user", content="just this"),), model="claude-sonnet-4-5"
    )
    await adapter.complete(request)

    body = captured["body"]
    assert "system" not in body
    assert "temperature" not in body
    assert body["messages"] == [{"role": "user", "content": "just this"}]
    assert set(body.keys()) == {"model", "messages", "max_tokens"}


async def test_complete_raises_on_http_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    adapter = AnthropicAdapter(api_key="bad-key", client=_make_client(handler))
    request = LLMRequest(
        messages=(LLMMessage(role="user", content="hi"),), model="claude-sonnet-4-5"
    )
    with pytest.raises(RuntimeError, match="401"):
        await adapter.complete(request)


async def test_complete_raises_on_only_system_messages() -> None:
    """The adapter itself rejects requests containing only system-role messages before ever
    calling the API, since filtering those out would leave an empty `messages` list -- which
    Anthropic's API requires to be non-empty."""
    adapter = AnthropicAdapter(
        api_key="sk-test", client=_make_client(lambda r: httpx.Response(200))
    )
    request = LLMRequest(
        messages=(LLMMessage(role="system", content="be terse"),), model="claude-sonnet-4-5"
    )
    with pytest.raises(ValueError, match="non-system message"):
        await adapter.complete(request)


async def test_complete_raises_on_unsupported_role() -> None:
    """Anthropic's API only accepts 'user'/'assistant' outside `system`; a message with any
    other role (e.g. an OpenAI-shaped 'function'/'tool' role) must be rejected clearly rather
    than sent to the API and failing with an opaque 400."""
    adapter = AnthropicAdapter(
        api_key="sk-test", client=_make_client(lambda r: httpx.Response(200))
    )
    request = LLMRequest(
        messages=(LLMMessage(role="tool", content="42"),), model="claude-sonnet-4-5"
    )
    with pytest.raises(ValueError, match="'tool'"):
        await adapter.complete(request)


async def test_complete_raises_runtimeerror_on_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    adapter = AnthropicAdapter(api_key="sk-test", client=_make_client(handler))
    request = LLMRequest(
        messages=(LLMMessage(role="user", content="hi"),), model="claude-sonnet-4-5"
    )
    with pytest.raises(RuntimeError, match="non-JSON"):
        await adapter.complete(request)


async def test_complete_joins_multiple_text_blocks_with_newline() -> None:
    """Multiple `text` content blocks (e.g. separated by a `tool_use` block) must not be
    concatenated with no separator, which would merge unrelated sentences together."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "claude-sonnet-4-5",
                "content": [
                    {"type": "text", "text": "Let me search."},
                    {"type": "tool_use", "id": "t1", "name": "search", "input": {}},
                    {"type": "text", "text": "The result is 42."},
                ],
            },
        )

    adapter = AnthropicAdapter(api_key="sk-test", client=_make_client(handler))
    request = LLMRequest(
        messages=(LLMMessage(role="user", content="hi"),), model="claude-sonnet-4-5"
    )
    response = await adapter.complete(request)

    assert response.content == "Let me search.\nThe result is 42."
