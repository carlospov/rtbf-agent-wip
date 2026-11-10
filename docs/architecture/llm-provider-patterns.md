# LLM provider abstraction: how Open Code Review does it, and what we take from it

This repo needs an `LLMProvider` abstraction that satisfies
[`R16`-`R19`](../planning/RESTRICTIONS.md) (core never imports a concrete SDK; minimum providers
Anthropic/OpenAI/OpenRouter/opencode/generic-OpenAI-compatible; local-only config, works fully
offline against `localhost`; documented minimum-data-sent) — see
[`O3`](../planning/OBJECTIVES.md) and the Phase 0/2 items in [`tasks/todo.md`](../../tasks/todo.md)
that implement it. [Open Code Review](https://github.com/alibaba/open-code-review) (OCR, wired up
for this repo's CI in `docs/ci/code-review-setup.md`) solves the identical problem — "support
~15 LLM vendors behind one interface, configured entirely outside the repo" — at production scale
inside Alibaba. Its Go source (`internal/llm/{client,providers,resolver}.go`) is worth reading
before implementing our own, because the design answer turns out to be simpler than "one adapter
per vendor."

## What OCR actually does

**One interface, two protocol implementations, not N vendor implementations.**

```go
type LLMClient interface {
    CompletionsWithCtx(ctx context.Context, req ChatRequest) (*ChatResponse, error)
}
```

Only two concrete types satisfy it: `AnthropicClient` (wraps `anthropic-sdk-go`, talks the
Messages API shape) and `OpenAIClient` (wraps `openai-go`, talks Chat Completions). Every vendor
OCR supports — OpenAI, DeepSeek, Z.AI, Kimi/Moonshot, MiniMax, Alibaba DashScope, Tencent
TokenHub, Baidu Qianfan, Volcano Engine, Eden AI, and any private gateway — reuses one of those
two clients, because nearly every non-Anthropic vendor now exposes an OpenAI-compatible
`/chat/completions` surface. The vendor-specific bit is never new client code; it's a data record:

```go
type Provider struct {
    Name, DisplayName string
    Protocol          string   // "anthropic" or "openai" — the only two that exist
    BaseURL           string
    AuthHeader        string   // usually empty; only Anthropic needs "x-api-key" vs "authorization"
    EnvVar            string   // e.g. "ANTHROPIC_API_KEY" — picked up automatically if already set
    Models            []string
}
```

`internal/llm/providers.go` is a flat `[]Provider{...}` literal — ~15 entries, each 5-8 lines, no
logic. Adding a 16th vendor is a data-only PR as long as it speaks one of the two protocols.
Anything genuinely different (a private gateway, a self-hosted endpoint) is a "custom provider" —
same `{url, protocol, api_key}` shape, just not baked into the registry — so `localhost:11434`
(Ollama) needs zero code, only a user-supplied `url`/`protocol` pair (satisfies our R18 directly).

**Config resolution is a priority-ordered strategy chain, not a single source.** OCR tries, in
order: its own config file (`~/.opencodereview/config.json`) → its own env vars (`OCR_LLM_URL`
etc.) → Claude Code's env vars (`ANTHROPIC_BASE_URL` etc., so users who already configured Claude
Code get OCR "for free") → known shell rc files. First strategy that yields a complete
`{url, token, model}` triple wins. Nothing is ever read from a file inside the repo.

**The interface stays narrow on purpose.** `ChatRequest`/`ChatResponse`/`Message` are protocol-
agnostic; only `Protocol == "anthropic"` triggers Anthropic-shaped content-block handling
(`ContentBlock` with `tool_result` nesting) inside the two client structs, never leaking into
caller code. Callers (the review agent loop) only ever see `LLMClient.CompletionsWithCtx`.

## Applying this to our `LLMProvider`

Recommendation for `src/rtbf_agent/core/interfaces.py` (Phase 0) and the adapters that implement
it (Phase 2, `src/rtbf_agent/plugins/llm/`):

1. **`LLMProvider` ABC gets one method** (e.g. `complete(request: LLMRequest) -> LLMResponse`),
   mirroring OCR's single-method `LLMClient`. Resist adding vendor-shaped methods to it — that's
   the leak OCR's design specifically avoids.
2. **Two concrete adapters, not five.** `AnthropicAdapter` and `OpenAICompatibleAdapter` cover
   Anthropic, OpenAI, OpenRouter, and any self-hosted OpenAI-compatible endpoint (Ollama/LM
   Studio/vLLM) — all four of R17's non-Anthropic entries speak the same wire protocol. `opencode`
   (via opencode Zen, see `docs/ci/code-review-setup.md`) also exposes an OpenAI-compatible
   surface for most models, so it's a config entry, not a third adapter — confirm this holds for
   whichever opencode model gets used before assuming zero new code, same caveat noted in the CI
   setup doc.
3. **A small provider registry as data, not code** — a `dict[str, ProviderPreset]` (or a handful
   of frozen dataclasses) holding `{base_url, protocol, env_var, models}` per known provider,
   exactly like OCR's `registry []Provider`. Discharges R21 (`ocr`'s 15 vendors as ~15 data
   entries is the existence proof that adding a provider never needs new adapter code).
4. **Config resolution: env vars first, local gitignored config file second, nothing else.**
   OCR's 4-strategy chain is more elaborate than we need (we don't need to piggyback on another
   tool's env vars); a 2-step chain (`RTBF_LLM_*` env vars → `~/.config/rtbf-agent/config.toml` or
   similar) is enough to satisfy R18 while staying simple. Both strategies must resolve fully
   offline when pointed at `localhost` — no code path may assume a strategy that isn't the active
   one is unavailable (e.g. no hardcoded fallback to a cloud default).
5. **What we deliberately don't copy:** OCR's Claude-Code-env-piggybacking strategy (not relevant,
   we're not nested inside another CLI's config) and its shell-rc-parsing fallback (an extra
   attack surface / footgun not worth it for a security-sensitive tool per R6 principles).

## Test implications (ties to `REQUIREMENTS_TRACE.md`)

- `test_core_has_no_direct_llm_sdk_import` (R16) — OCR's own `client.go` is the negative example:
  vendor SDK imports (`anthropic-sdk-go`, `openai-go`) live only in the two client files, never in
  the agent/orchestration code that calls `LLMClient`. Our equivalent: vendor SDK imports confined
  to `plugins/llm/*`, never `core/`.
- `test_<provider>_adapter_implements_llmprovider_contract` (R17) — one contract test per adapter,
  not per registry entry; registry entries are pure data and don't need their own test beyond
  `test_provider_registry_entries_have_required_fields`.
- `test_localhost_configured_provider_makes_no_external_network_calls` (R18) — provable the same
  way OCR's custom-provider path proves it: point `base_url` at a local stub server, block network
  in the test sandbox, assert the call still succeeds.

## Same shape, reused for web search

`core/interfaces.py`'s `WebSearchProvider` (issue #65, prerequisite for #63/#64) is a direct copy
of this design's *shape* — one narrow method, no vendor-shaped methods, config resolved entirely
below the interface — applied to a different plugin family. The provider-selection reasoning
differs, though: there's no OCR-style "two protocols cover every vendor" simplification for web
search, and no registry of hosted presets, because the one concrete adapter shipped
(`plugins/websearch/searxng.py`, talking a self-hosted SearXNG instance's JSON API) deliberately
avoids a hosted third-party vendor at all, for the same "your own X, not a project-operated relay"
reason `plugins/senders/smtp.py` uses your own SMTP account. See that module's docstring for the
full provider comparison (Brave Search API vs. SearXNG) and rationale.
