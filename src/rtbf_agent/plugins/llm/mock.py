"""`MockLLMProvider` — a deterministic, network-free `LLMProvider` (R16, R17).

Phase 1 (spec S9) proves the discover -> classify -> draft -> followup -> notify shape works
end-to-end against mocks before any real connector exists. This is the `LLMProvider` half of
that: it returns canned, deterministic responses and makes no network calls at all, so the
mocked agent loop can run without credentials, without the internet, and without pulling a
concrete SDK into `core/` (R16) or anywhere near it.

Not a real provider: no vendor wire protocol, no retries, no token accounting — that's Phase 2's
job (`plugins/llm/*` real adapters). This module exists purely so `LLMProvider` has at least one
concrete implementation usable end-to-end without touching `core/` (R17).
"""

from __future__ import annotations

from rtbf_agent.core.interfaces import LLMProvider, LLMRequest, LLMResponse


class MockLLMProvider(LLMProvider):
    """Deterministic stand-in for a real `LLMProvider` adapter.

    `complete` never touches the network: it derives a fixed, reproducible response from the
    request's `model` and last message content only, so tests and the Phase 1 mocked agent loop
    get the same output for the same input every run.
    """

    def __init__(self, response_prefix: str = "[mock]") -> None:
        self._response_prefix = response_prefix

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Return a deterministic `LLMResponse` derived from `request` — no network call.

        The canned content echoes the last message's content so callers/tests can assert on it
        without the provider needing any real reasoning; `raw` is left at its default empty
        mapping since there is no vendor payload to carry.
        """
        last_content = request.messages[-1].content if request.messages else ""
        content = f"{self._response_prefix} {last_content}".strip()
        return LLMResponse(content=content, model=request.model)
