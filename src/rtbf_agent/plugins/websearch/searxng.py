"""`SearXNGProvider` — the first (and, as of this writing, only) concrete `WebSearchProvider`.

## Why SearXNG

`#63`/`#64` need current, per-site information (a specific company's privacy contact, the
jurisdiction its terms of service claim) that an LLM's training data is generically unreliable
for, especially for smaller or newer sites. That means *some* web-search capability, chosen the
same way this project chose its LLM/SMTP/Telegram integrations: no centralized backend or
third-party telemetry this project operates (R12/R13), and preferably a provider whose own
business model doesn't profile the user either -- the same "point it at your own X" shape this
project already uses for outbound email (`plugins/senders/smtp.py`, your own SMTP account) and for
a local LLM endpoint (`plugins/llm/openai_compatible.py`, e.g. your own Ollama/vLLM instance).

Providers actually considered (checked live, not from training-data memory, 2026-07-15):

- **Brave Search API** — the option the tracking issue itself suggested, and previously the
  obvious pick (a real no-tracking policy, a 2,000 query/month free tier). As of this writing it
  no longer has a free tier: Brave replaced it in February 2026 with prepaid metered credits
  (roughly $0.003-0.005/query, a card required up front, and attribution required in exchange for
  the initial $5 credit). Still cheap and still no-tracking, but it's a paid third-party API a
  self-hosting user has to hand a card to just to try this feature -- a worse fit for a personal,
  privacy-conscious tool than an option with zero recurring cost and zero third party at all.
- **SearXNG** (chosen) — a free, open-source federated metasearch engine you run yourself (a
  single Docker container). It aggregates results from many upstream engines without SearXNG
  itself logging, profiling, or reselling your queries, has no API key requirement, no billing,
  and no request quota beyond your own instance's capacity. This is the same "self-hosted, your
  own instance, no project-operated relay" shape as the existing SMTP/local-LLM integrations
  (R12/R13) — not just brand alignment with "privacy-respecting" but the actual strongest fit for
  this project's own non-negotiables. The tradeoff, documented plainly: it requires the user to
  run an instance (or point at one they already trust) with the JSON output format enabled, since
  many public instances disable it by default (`docs/architecture` precedent: the same
  local-endpoint tradeoff `plugins/llm/openai_compatible.py` already asks of a self-hosted Ollama
  user).

If a hosted, keyed vendor (Brave, or another) becomes the better fit later, it's a new
`plugins/websearch/` module implementing `WebSearchProvider` -- this ABC and `core/interfaces.py`
need no change (R21).

## Wire shape

Calls SearXNG's `GET {base_url}/search?q=<query>&format=json` endpoint directly over `httpx`
(`docs/dev/search_api.html` -- SearXNG must have `search: formats: [json]` enabled; most public
instances disable this, so this adapter is meant for an instance you control). No third-party
Python SDK exists for this wire shape, so -- like `plugins/llm/anthropic.py` -- this adapter talks
the REST/JSON API directly rather than adding a dependency for a handful of lines of HTTP code.

## Testing against a real instance

`tests/plugins/websearch/test_searxng.py` proves the request/response translation logic against
`httpx.MockTransport` -- fast, no external dependency, what runs on every `pytest` invocation.
`tests/plugins/websearch/test_searxng_integration.py` is the deliberate exception: one real query
against a real, running SearXNG instance (which itself queries real upstream engines/websites),
skipped unless `RTBF_SEARXNG_BASE_URL` is set. CI provides that instance itself (a throwaway
container built from `.github/searxng/settings.yml`) so this runs on every PR, not just locally.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from rtbf_agent.core.interfaces import SearchResult, WebSearchProvider
from rtbf_agent.plugins.websearch.config import SearXNGConfig, resolve_searxng_config

DEFAULT_TIMEOUT_SECONDS = 30.0


class SearXNGProvider(WebSearchProvider):
    """Talks a SearXNG instance's JSON search API. The only concrete `WebSearchProvider` (R17-style
    minimum: exactly one required adapter to start, more addable without touching the ABC)."""

    def __init__(
        self,
        config: SearXNGConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """`config` defaults to `resolve_searxng_config()` (env vars, then local config file).

        `client` is an injection point for tests (an `httpx.AsyncClient` with a
        `httpx.MockTransport`, or one pointed at a local stub server) -- production callers omit
        it and a short-lived client is created per call, mirroring
        `plugins/llm/anthropic.py`'s `AnthropicAdapter`.
        """
        self._config = config if config is not None else resolve_searxng_config()
        self._client = client
        self._timeout = timeout

    async def search(self, query: str) -> list[SearchResult]:
        """Return SearXNG's ranked results for `query`, most relevant first.

        Never raises on "nothing found" -- an empty `results` array yields an empty list (R20).
        Raises `RuntimeError` if the instance returns a non-2xx status or a non-JSON body (a
        misconfigured/unreachable instance is a real failure, not a "no results" case).
        """
        headers = {"Accept": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"

        base_url = self._config.base_url.rstrip("/")
        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
        try:
            try:
                response = await client.get(
                    f"{base_url}/search",
                    params={"q": query, "format": "json"},
                    headers=headers,
                )
            except httpx.RequestError as exc:
                # Connection refused/timed out/DNS failure, etc. -- the same "unreachable
                # instance" failure mode the docstring already documents for a non-2xx status,
                # just at the transport layer instead of the HTTP layer.
                raise RuntimeError(f"SearXNG instance unreachable at {base_url}: {exc}") from exc
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"SearXNG instance returned {exc.response.status_code}: "
                    f"{exc.response.text[:500]}"
                ) from exc
            try:
                parsed: Any = response.json()
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"SearXNG instance returned a non-JSON body (is 'format: json' enabled on "
                    f"this instance? see this module's docstring): {response.text[:500]}"
                ) from exc
            if not isinstance(parsed, dict):
                # `response.json()` can return any valid JSON value, not only an object --
                # a misconfigured/unexpected instance response is a real failure (R20: never
                # silently misinterpret a vendor's wire shape), not a "no results" case.
                raise RuntimeError(
                    f"SearXNG instance returned a JSON {type(parsed).__name__}, expected an "
                    f"object: {response.text[:500]}"
                )
            data: dict[str, Any] = parsed
        finally:
            if owns_client:
                await client.aclose()

        return [
            # `.get(field) or ""`, not `str(.get(field, ""))`: some upstream engines return
            # JSON `null` (not a missing key) for a field SearXNG couldn't populate, and
            # `str(None)` would silently produce the literal text "None" in a result a caller
            # might treat as real content (e.g. #63/#64 feeding this into an LLM prompt).
            SearchResult(
                title=item.get("title") or "",
                url=item.get("url") or "",
                snippet=item.get("content") or "",
            )
            for item in data.get("results", [])
        ]
