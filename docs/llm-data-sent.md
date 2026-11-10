# What data crosses the wire to an LLM provider (R19)

This page enumerates, for each `LLMProvider` adapter in `src/rtbf_agent/plugins/llm/`, exactly
what leaves this machine when `complete()` is called — so a self-hosting, privacy-conscious user
can audit it without reading the adapter source. It covers the adapter layer only (issue #16);
call-site scoping — e.g. a future "classify" step sending only domain + page metadata, never a
raw email body — is a separate, not-yet-built concern (`agent_loop.py`, issue #10) and will get
its own section here once that code exists, per `docs/planning/REQUIREMENTS_TRACE.md`'s R19 entry.

## The rule both adapters follow

Neither adapter adds, infers, or injects anything beyond what the caller put on the `LLMRequest`
it was given (`src/rtbf_agent/core/interfaces.py`). Concretely:

- `request.messages` (`role` + `content` only) is sent, translated into the wire protocol's shape
  — never summarized, logged elsewhere, or supplemented with extra context.
- `request.model` selects which model the provider runs; it does not add capability flags.
- `request.max_tokens` / `request.temperature` are forwarded only if the caller set them —
  omitted from the wire payload entirely otherwise (`AnthropicAdapter` is the one exception:
  Anthropic's Messages API requires `max_tokens` on every call, so a default of `1024` is used if
  the caller didn't supply one — see `AnthropicAdapter`'s docstring).
- No adapter attaches a default system prompt, telemetry, request ID, or user-identifying header
  beyond the API key needed to authenticate.

Neither adapter reads or sends anything from outside the `LLMRequest` object — no case data, no
file contents, no environment variables beyond the credential resolved by
`rtbf_agent.plugins.llm.config.resolve_llm_config` for the `Authorization`/`x-api-key` header
itself.

## `AnthropicAdapter` (`plugins/llm/anthropic.py`)

Talks `POST {base_url}/v1/messages` (Anthropic Messages API).

| Sent | Not sent |
| --- | --- |
| `messages` with `role != "system"`, as `{role, content}` pairs | Any field not on `LLMRequest` |
| `system` — the concatenation of all `role == "system"` message contents, only if at least one was given | A default/injected system prompt |
| `model` | — |
| `max_tokens` (caller's value, or `1024` if unset — required by the API) | — |
| `temperature`, only if the caller set one | — |
| Headers: `x-api-key` (the resolved API key), `anthropic-version`, `content-type` | Any other header, cookie, or tracking identifier |

## `OpenAICompatibleAdapter` (`plugins/llm/openai_compatible.py`)

Talks `POST {base_url}/chat/completions` (OpenAI Chat Completions wire shape) — the same class
handles OpenAI, OpenRouter, opencode Zen, and any self-hosted OpenAI-compatible endpoint
(Ollama/LM Studio/vLLM); only the `base_url`/`api_key` constructor arguments differ per provider.

| Sent | Not sent |
| --- | --- |
| `messages`, all roles included as `{role, content}` pairs | Any field not on `LLMRequest` |
| `model` | — |
| `max_tokens`, only if the caller set one | No default injected (unlike the Anthropic adapter — this protocol doesn't require it) |
| `temperature`, only if the caller set one | — |
| Header: `Authorization: Bearer <api-key>`, only if an API key was configured | Nothing at all when `api_key` is `None` — the common case for a local Ollama/LM Studio endpoint (R18) |

## Configuration itself never touches the network

`rtbf_agent.plugins.llm.config.resolve_llm_config` (R18) resolves `{protocol, base_url, api_key,
model}` from environment variables, then an optional local TOML file
(`~/.config/rtbf-agent/config.toml` by default) — both steps are local filesystem/env reads, no
network call. When `base_url` points at `localhost`/`127.0.0.1`, nothing in either adapter or the
config resolver attempts to reach any other host; see
`tests/plugins/llm/test_network_isolation.py::test_localhost_configured_provider_makes_no_external_network_calls`
for the enforced proof (DNS resolution to any non-local host is blocked for the duration of that
test, and the adapter still completes a full request/response cycle against a real local stub
server).
