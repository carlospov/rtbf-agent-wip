"""Tests for `rtbf_agent.plugins.websearch.searxng.SearXNGProvider` (R16, R20).

Uses `httpx.MockTransport` (no real network call, ever) to fake a SearXNG instance's JSON search
API and assert `SearXNGProvider` translates the request/response correctly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest

from rtbf_agent.core.interfaces import SearchResult, WebSearchProvider
from rtbf_agent.plugins.websearch.config import SearXNGConfig
from rtbf_agent.plugins.websearch.searxng import SearXNGProvider


@asynccontextmanager
async def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> AsyncIterator[httpx.AsyncClient]:
    """Mock-transport `AsyncClient`, closed on exit — the provider never closes an injected
    client (ownership stays with the caller), so tests must close it themselves."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        yield client


def test_searxng_adapter_implements_websearchprovider_contract() -> None:
    provider = SearXNGProvider(SearXNGConfig(base_url="http://localhost:8080"))
    assert isinstance(provider, WebSearchProvider)


async def test_search_translates_request_and_response_correctly() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Example Corp Privacy Policy",
                        "url": "https://example.invalid/privacy",
                        "content": "Example Corp's privacy contact is...",
                    },
                    {
                        "title": "Example Corp Terms",
                        "url": "https://example.invalid/terms",
                        "content": "Governed by the laws of...",
                    },
                ]
            },
        )

    async with _make_client(handler) as client:
        provider = SearXNGProvider(
            SearXNGConfig(base_url="http://localhost:8080", api_key="test-token"),
            client=client,
        )
        results = await provider.search("example corp privacy contact")

    assert captured["url"].startswith("http://localhost:8080/search?")
    assert "q=example" in captured["url"]
    assert captured["headers"]["authorization"] == "Bearer test-token"
    assert len(results) == 2
    assert isinstance(results[0], SearchResult)
    assert results[0].title == "Example Corp Privacy Policy"
    assert results[0].url == "https://example.invalid/privacy"
    assert results[0].snippet == "Example Corp's privacy contact is..."


async def test_search_omits_authorization_header_when_no_api_key_configured() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"results": []})

    async with _make_client(handler) as client:
        provider = SearXNGProvider(SearXNGConfig(base_url="http://localhost:8080"), client=client)
        await provider.search("anything")

    assert "authorization" not in captured["headers"]


async def test_search_returns_empty_list_for_no_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    async with _make_client(handler) as client:
        provider = SearXNGProvider(SearXNGConfig(base_url="http://localhost:8080"), client=client)
        results = await provider.search("nothing matches this")

    assert results == []


async def test_search_treats_a_null_field_as_empty_string_not_the_text_none() -> None:
    """Some upstream engines return JSON `null` (not a missing key) for a field they couldn't
    populate -- `str(None)` would wrongly surface the literal text "None" as if it were content."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [{"title": None, "url": "https://example.invalid", "content": None}]},
        )

    async with _make_client(handler) as client:
        provider = SearXNGProvider(SearXNGConfig(base_url="http://localhost:8080"), client=client)
        results = await provider.search("anything")

    assert results[0].title == ""
    assert results[0].snippet == ""


async def test_search_raises_runtimeerror_on_transport_error() -> None:
    """A connection failure (refused/timed out/DNS) is the same "unreachable instance" failure
    mode as a non-2xx status -- it must surface as RuntimeError too, not a raw httpx exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _make_client(handler) as client:
        provider = SearXNGProvider(SearXNGConfig(base_url="http://localhost:8080"), client=client)
        with pytest.raises(RuntimeError, match="unreachable"):
            await provider.search("anything")


async def test_search_raises_runtimeerror_on_non_2xx_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal server error")

    async with _make_client(handler) as client:
        provider = SearXNGProvider(SearXNGConfig(base_url="http://localhost:8080"), client=client)
        with pytest.raises(RuntimeError, match="500"):
            await provider.search("anything")


async def test_search_raises_runtimeerror_on_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    async with _make_client(handler) as client:
        provider = SearXNGProvider(SearXNGConfig(base_url="http://localhost:8080"), client=client)
        with pytest.raises(RuntimeError, match="non-JSON"):
            await provider.search("anything")


async def test_search_raises_runtimeerror_on_a_non_object_json_body() -> None:
    """`response.json()` can return any valid JSON value, not only an object -- a top-level JSON
    array/string/number is a real failure to surface, not silently misread as "no results"."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["unexpected", "array"])

    async with _make_client(handler) as client:
        provider = SearXNGProvider(SearXNGConfig(base_url="http://localhost:8080"), client=client)
        with pytest.raises(RuntimeError, match="list"):
            await provider.search("anything")


async def test_base_url_trailing_slash_is_stripped() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"results": []})

    async with _make_client(handler) as client:
        provider = SearXNGProvider(SearXNGConfig(base_url="http://localhost:8080/"), client=client)
        await provider.search("anything")

    assert captured["url"].startswith("http://localhost:8080/search?")
    assert "//search" not in captured["url"]
