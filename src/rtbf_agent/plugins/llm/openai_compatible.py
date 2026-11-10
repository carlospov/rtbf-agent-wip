"""`OpenAICompatibleAdapter` — the OpenAI-protocol half of the two-adapter design (R16-R19).

Design precedent: OCR's `OpenAIClient` (`internal/llm/client.go`) is reused, unmodified, for
every vendor that exposes an OpenAI-compatible `/chat/completions` surface — OCR's own registry
covers OpenAI, DeepSeek, Z.AI, Kimi/Moonshot, MiniMax, DashScope, TokenHub, Qianfan, Volcano
Engine, Eden AI, and any private gateway this way, because "OpenAI-compatible" is a wire-protocol
property, not a per-vendor one. This adapter is the direct equivalent: OpenAI, OpenRouter, opencode
(via opencode Zen — verified live 2026-07-12, see `docs/ci/code-review-setup.md`), and any
self-hosted OpenAI-compatible endpoint (Ollama/LM Studio/vLLM) all reuse this one class, keyed only
by which `base_url` and API key a caller supplies (`docs/architecture/llm-provider-patterns.md`,
R17). Talks raw HTTP over `httpx` rather than the `openai` SDK, for the same reason
`AnthropicAdapter` avoids the `anthropic` SDK — the wire shape is small and this keeps the only
third-party dependency shared across both adapters.

**What crosses the wire (R19):** exactly `request.messages` (role + content only, no extra
fields), `request.model`, and `request.max_tokens`/`request.temperature` if the caller set them.
Nothing else. See `docs/llm-data-sent.md` for the full per-adapter accounting.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from rtbf_agent.core.interfaces import LLMProvider, LLMRequest, LLMResponse

DEFAULT_TIMEOUT_SECONDS = 120.0
"""httpx's own default (5s) is tuned for typical REST calls, not LLM inference latency, which can
run 30-120s for longer completions."""


class OpenAICompatibleAdapter(LLMProvider):
    """Talks the OpenAI Chat Completions wire protocol. Covers OpenAI, OpenRouter, opencode Zen,
    and any self-hosted OpenAI-compatible endpoint (R17) — parametrized entirely by `base_url`,
    never by vendor-specific code (R18: a `base_url` of `http://localhost:11434/v1` needs no new
    class, only a different constructor argument)."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """`api_key` is optional — many self-hosted endpoints (Ollama, LM Studio) don't require
        one; `client` is an injection point for tests, same as `AnthropicAdapter`."""
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client

    async def complete(self, request: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature

        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"

        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
        try:
            response = await client.post(
                f"{self._base_url}/chat/completions", json=payload, headers=headers
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"API returned {exc.response.status_code}: {exc.response.text}"
                ) from exc
            try:
                data: dict[str, Any] = response.json()
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"API returned non-JSON body: {response.text[:500]}") from exc
        finally:
            if owns_client:
                await client.aclose()

        choices = data.get("choices")
        if not choices:
            raise ValueError(f"API returned no choices; unexpected response shape: {data}")
        raw_content = choices[0].get("message", {}).get("content")
        content = raw_content if isinstance(raw_content, str) else ""
        return LLMResponse(content=content, model=data.get("model", request.model), raw=data)
