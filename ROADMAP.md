# Roadmap

Phased plan with a verifiable done-criterion per phase (R29). The concrete, current-state
checklist lives in [`tasks/todo.md`](tasks/todo.md) — this document is the higher-level "what
phase are we in and how do we know it's done" view; `tasks/todo.md` is the "what's the next
checklist item" view. When they disagree, `tasks/todo.md` is more current (it's updated per
change); this file is updated per phase transition.

## Phase 0 — Foundations ✅ done

**Done-criterion:** `core/interfaces.py` defines every plugin ABC (`DiscoverySource`,
`LegalTemplateProvider`, `Notifier`, `LLMProvider`) with no concrete logic, and `core/models.py`
defines the `Case`/`Event` schema (10-state kanban, insert-only audit log) — both with passing
tests proving no hardcoded jurisdiction/language logic exists in `core/` and the state model
supports indefinite waiting with no timeout field (R9).

Status: done. Both modules merged, tests passing, enforced by
`test_no_hardcoded_jurisdiction_or_language_literals_in_core` and
`test_core_has_no_direct_llm_sdk_import`.

## Phase 1 — Mocked agent loop ✅ done

**Done-criterion:** a full discover → classify → draft → notify → [human approves] → send cycle
runs end-to-end against `MockLLMProvider` and `ConsoleNotifier`, proven by
`tests/test_agent_loop_mock.py`, with the CI coverage gate raised from a placeholder `0` to a real,
enforced threshold, and a CI check (not just review habit) failing the build if any `core/`/
`plugins/` module lacks a test file.

Status: done. `agent_loop.py`, coverage gate at 90%, `test_every_core_and_plugin_module_has_a_test_file`
all merged and enforced.

## Phase 2 — Real connectors ✅ mostly done

**Done-criterion:** every mocked piece from Phase 1 has at least one real implementation, wired
together into a runnable command that completes one real case end-to-end — real discovery, real
LLM-backed classification, real jurisdiction-specific legal content, a real human approval gate,
and a real send — with nothing left mocked in the critical path.

Status: **the core milestone is done.** `python -m rtbf_agent` runs the full real pipeline:

- ✅ Gmail discovery (read-only OAuth scope)
- ✅ Real LLM adapters (Anthropic, OpenAI-compatible — Ollama/LM Studio/vLLM/OpenRouter/opencode
  all work through the same adapter)
- ✅ Real GDPR + English legal-template content (jurisdiction and drafting language as independent
  axes, proven extensible without touching either's plugin when adding the other)
- ✅ Minimal i18n contract (`I18nProvider`, one concrete resource-backed implementation)
- ✅ Real SMTP `Sender`, gated behind the same approval-event guard as everything else
- ✅ Runnable CLI entrypoint tying all of the above together, safe to re-run repeatedly (R9)

Remaining in this phase, not yet done:

- ⏳ Migrate `agent_loop.py` from synchronous `sqlmodel.Session` to an async session, before any
  additional real connector introduces genuine concurrent I/O this loop needs to interleave with
- ⏳ Recipient resolution: the real send currently targets the scanned mailbox itself, not the
  discovered site's actual privacy/DPO contact — tracked in
  issue #52, not yet fixed. Manual per-case
  jurisdiction/language override at case-creation time is done (issue #43); Spanish identifiers
  were translated to English (issue #45); `.env.example` documents the real variable names
  (issue #42).

## Phase 3 — Real-world usability

**Done-criterion:** a non-technical user (not just the maintainer) can run this against their own
accounts using the primary intended UX (Telegram + kanban, not a developer console), with sensitive
documents handled per the spec's strictest data-minimization requirement, and community
contribution is documented and structured for outside participation.

Status: mostly done.

- ✅ Telegram bot notifier (issue #14) — approve/reject via inline-keyboard buttons, not free-text
  parsing; `chat_id` whitelisted from the very first message, no bootstrap exception window;
  document receipt (encrypted storage/lifecycle for received documents is separate, see below).
  Wired into the CLI as a selectable notifier backend (issue #60).
- ✅ Kanban web UI (issues #53/#54/#55) — FastAPI + server-rendered Jinja2, browsable case board
  grouped by state, plus a per-case event audit trail (including the full drafted request text —
  the first place a human can review a draft before approving it via Telegram). Reads the same
  local database as the CLI/Telegram bot, no independent state, no drift.
- ✅ `CONTRIBUTING.md`, `docs/i18n/`, and a public [user guide](../docs/user-guide/README.md)
  (issue #50) for community contribution and end-user setup respectively.
- ⏳ Sensitive document lifecycle — encrypted temp storage, secure delete on send, TTL purge if
  never approved. Not started (issue #17).
- ⏳ No approve/reject action on the kanban UI yet — Telegram/console remains the only approval
  surface; the kanban board is explicitly a view + audit trail first, per spec.

## Ongoing, not phase-bound

- The manual-verification-restrictions checklist (README's no-absolute-deletion/own-data-only
  language, no committed secrets, no telemetry/centralized backend, clean commit history) is
  re-checked at each PR, not a one-time task — see `tasks/todo.md`'s "Manual-verification-only
  restrictions" section for the full list.
- A second jurisdiction and a second drafting language, proving the independent-axis architecture
  holds for real (not just via a throwaway test stub) — no committed timeline, opportunistic.
