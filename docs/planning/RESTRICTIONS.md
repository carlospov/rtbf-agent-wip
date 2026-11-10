# Restrictions

Numbered, traceable constraints (`R1`, `R2`, ...) pulled from the requirements spec's non-negotiable
sections. Each is written so a later planning session can map it 1:1 to a functional requirement
and at least one test, cited by this ID. Nothing here is invented — every restriction traces back
to a specific section of
[`docs/specs/2026-07-12-rtbf-agent-requirements.md`](../specs/2026-07-12-rtbf-agent-requirements.md)
(cited per section below). This file intentionally does not say *how* to enforce any restriction —
that translation happens in the next planning pass, before any code is written.

---

## Human-in-the-loop / irreversibility (spec §2.1, §7)

- **R1** — No irreversible action (sending a request, attaching a document, signing anything)
  executes without explicit human approval at the moment of execution — approval given earlier
  (e.g. at draft time) does not substitute for approval at send-time.
- **R2** — The agent never resolves identity verification on its own: it does not fill identity
  forms, does not sign documents, does not generate or edit identification images.
- **R3** — Every proposal the agent makes — including ones the human does not approve — is
  recorded, not just the ones that execute (overlaps with R4; called out separately because it's
  easy to only log executed actions).

## Audit & logging (spec §2.2, §3.2)

- **R4** — Every agent action is recorded in an append-only, immutable log with actor, timestamp,
  and detail.
- **R5** — Audit log entries involving sensitive documents never contain document content — only a
  hash and metadata (e.g. "attached", "sent").

## Sensitive data minimization (spec §2.3, §3.3, §3.4)

- **R6** — Sensitive documents (e.g. identity documents) are never written to disk in plaintext —
  held in memory or in encrypted temporary storage only.
- **R7** — After being attached and sent, a sensitive document is deleted via secure delete
  (overwrite), not merely unlinked/logically deleted.
- **R8** — A sensitive document not approved for sending within a configurable TTL (default
  24–48h) is purged automatically; the user is asked to re-upload if still needed. Not accumulating
  sensitive data at rest takes priority over user convenience.
- **R9** — A `Case` in a waiting state has no timeout, auto-close, or auto-escalation. The only
  permitted automated behavior is an opt-in, off-by-default "N days without updates" notice that
  never itself changes `Case` state or triggers any resend.

## Platform boundaries / transparency (spec §2.4, §3.4.6, §7)

- **R10** — The project does not claim control or guarantees over data once it has transited
  third-party infrastructure (e.g. a file after passing through Telegram's Bot API) — this
  limitation must be documented explicitly, not silently assumed.
- **R11** — No guarantee of deletion on third-party infrastructure (Telegram, email providers)
  beyond what that platform itself allows.

## Decentralization / privacy (spec §2.5, §2.6, §4, §7)

- **R12** — No centralized backend or service operated by the project's author. Everything runs
  locally using the user's own credentials (their own Telegram bot via @BotFather, their own email
  access).
- **R13** — Zero telemetry sent to the project's author.
- **R14** — Secrets (Telegram bot token, LLM API keys, etc.) are never committed to the repo;
  `.gitignore` covers them from the initial scaffold onward.
- **R15** — The Telegram bot enforces a `chat_id` whitelist starting from the very first message —
  any non-whitelisted chat is ignored, not just chats after initial setup.

## LLM provider abstraction (spec §0.1)

- **R16** — Core code never imports a concrete LLM SDK (Anthropic, OpenAI, etc.) directly outside
  of designated provider-adapter modules; all LLM calls go through an abstract provider interface.
- **R17** — Minimum supported providers at launch: Claude (Anthropic API), OpenAI, OpenRouter,
  opencode, and any OpenAI-API-compatible endpoint (covering self-hosted Ollama/LM Studio/vLLM as a
  first-class, not "extra," use case).
- **R18** — Provider configuration (API key, base URL, model) lives in local config/env only, never
  in the repo. The system must work fully, including with zero outbound internet access, when
  configured against a `localhost` endpoint.
- **R19** — Only the minimum data necessary for the specific task (classify/draft/etc.) is sent to
  the LLM, and what is/isn't sent must be documented clearly enough that a self-hosting,
  privacy-conscious user can audit it without reading the source.

## Extensibility / no hardcoding (spec §0 "regla de oro", §2.7)

- **R20** — No site-, language-, or jurisdiction-specific logic lives in `core/` — anything that
  only makes sense for one language, country, or site must be a plugin/adapter.
- **R21** — Adding a new language, jurisdiction, email provider, or site type requires only adding
  a new plugin module — never a change to core.

## Internationalization (spec §0, §6)

- **R22** — Interface language, drafting language, and legal jurisdiction are three independently
  configurable settings; no code path assumes any two of them are coupled or derivable from one
  another.
- **R23** — Every interface string (kanban UI + bot messages) is sourced from a
  translation/resource system — never a literal string embedded in code.
- **R24** — Legal request templates are parametrized on two independent axes: jurisdiction and
  drafting language — not a single combined axis.

## Scope boundaries (spec §2.6, §7)

- **R25** — The system is built and licensed for a user managing their own data only. The README
  must state explicitly that it is not intended for managing deletion of third parties' data
  without their explicit consent.

## Engineering standard (spec §0.1 "estándar de ingeniería")

- **R26** — No new module under `core/` or `plugins/` ships without tests: unit tests for core
  logic (discovery, classifier, drafting, followup), integration tests for full flows (with mocks
  for LLM, Telegram, and email).
- **R27** — CI fails the pipeline on lint failure or on a coverage regression below the configured
  threshold.
- **R28** — Documentation (README, architecture docs, config guides) is updated in the same PR as
  the code change it describes — documentation drift is treated as a bug, not a follow-up task.
- **R29** — Development follows an explicit, phased roadmap with a verifiable "done" criterion per
  phase, to prevent scope creep.
- **R30** — Commit/PR history uses a clean, conventional-commit-style structure — no accumulated
  "wip" commits left on `main`.

---

## Next step (not this session)

Translate each `R#` above into: (a) a concrete functional requirement, and (b) at least one test
(unit, integration, or a manual verification step where a test can't practically cover it — e.g.
R28's docs-in-same-PR is a review-process rule, not something a test asserts). Only once that
mapping exists does implementation start, per the ideas-hub workflow.
