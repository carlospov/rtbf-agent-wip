"""`AnthropicAdapter` — the Anthropic-protocol half of the two-adapter design (R16, R17, R19).

Design precedent: OCR's `AnthropicClient` (`internal/llm/client.go`) is one of exactly two
concrete `LLMClient` implementations; this adapter is its equivalent for `LLMProvider`. It talks
the Anthropic Messages API (`POST {base_url}/v1/messages`) directly over `httpx` rather than
pulling in the `anthropic` SDK — the wire shape is small enough that a raw HTTP call keeps this
adapter's only third-party dependency `httpx`, already needed by `OpenAICompatibleAdapter`, and
avoids adding a second heavyweight vendor SDK for one endpoint shape.

**What crosses the wire (R19):** exactly `request.messages` (translated to Anthropic's
`system` + `messages` split — content only, no extra fields), `request.model`, `request.max_tokens`
(Anthropic requires this field; defaults to 1024 if the caller didn't set one),
and `request.temperature` if the caller set one. Nothing else is added, inferred, or injected —
this adapter carries no default system prompt and no extra metadata. See `docs/llm-data-sent.md`
for the full per-adapter accounting.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from rtbf_agent.core.interfaces import LLMProvider, LLMRequest, LLMResponse

ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TIMEOUT_SECONDS = 120.0
"""httpx's own default (5s) is tuned for typical REST calls, not LLM inference latency, which can
run 30-120s for longer completions."""
VALID_NON_SYSTEM_ROLES = {"user", "assistant"}
"""The only two roles Anthropic's Messages API accepts outside the `system` field."""


class AnthropicAdapter(LLMProvider):
    """Talks the Anthropic Messages API. One of the two required protocol adapters (R17)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """`client` is an injection point for tests (an `httpx.AsyncClient` with a
        `httpx.MockTransport`, or one pointed at a local stub server) — production callers omit
        it and a short-lived client is created per call."""
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def complete(self, request: LLMRequest) -> LLMResponse:
        system_parts = [m.content for m in request.messages if m.role == "system"]
        wire_messages = []
        for m in request.messages:
            if m.role == "system":
                continue
            if m.role not in VALID_NON_SYSTEM_ROLES:
                raise ValueError(
                    f"Anthropic API supports only {sorted(VALID_NON_SYSTEM_ROLES)} roles "
                    f"(plus 'system'), got {m.role!r}"
                )
            wire_messages.append({"role": m.role, "content": m.content})
        if not wire_messages:
            raise ValueError(
                "Anthropic API requires at least one non-system message; request.messages "
                "contained only system messages or was empty"
            )

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": wire_messages,
            "max_tokens": request.max_tokens or DEFAULT_MAX_TOKENS,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.temperature is not None:
            payload["temperature"] = request.temperature

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }

        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
        try:
            response = await client.post(
                f"{self._base_url}/v1/messages", json=payload, headers=headers
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"Anthropic API returned {exc.response.status_code}: {exc.response.text[:500]}"
                ) from exc
            try:
                data: dict[str, Any] = response.json()
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Anthropic API returned non-JSON body: {response.text[:500]}"
                ) from exc
        finally:
            if owns_client:
                await client.aclose()

        content = "\n".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
        return LLMResponse(content=content, model=data.get("model", request.model), raw=data)
