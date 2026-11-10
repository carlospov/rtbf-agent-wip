# tasks/todo.md

Current, executable plan. Requirements come from
[`docs/planning/REQUIREMENTS_TRACE.md`](../docs/planning/REQUIREMENTS_TRACE.md), which translates
each restriction in [`RESTRICTIONS.md`](../docs/planning/RESTRICTIONS.md) (`R1`-`R30`) into a
functional requirement and a test (or a named manual-verification step). The "why" behind each
item lives in [`OBJECTIVES.md`](../docs/planning/OBJECTIVES.md). Per the ideas-hub workflow: spec
→ restrictions → requirements trace → this file → implementation, one commit per checklist item.
The higher-level "what phase are we in" view lives in [`ROADMAP.md`](../ROADMAP.md); this file is
the more current, per-change "what's the next checklist item" view.

Scope: get from scaffold (current state) to a mocked end-to-end agent loop, per
`docs/specs/2026-07-12-rtbf-agent-requirements.md` section 9.

## Phase 0 — plugin interfaces (no implementations yet)

- [x] `src/rtbf_agent/core/interfaces.py`: abstract base classes for `DiscoverySource`,
      `LegalTemplateProvider`, `Notifier` (`base_notifier`), `LLMProvider`. No concrete logic —
      just the contracts every plugin implements, each ABC's docstring naming the restriction(s)
      it exists to enforce.
      Discharges: R16, R20, R21 (contracts only; no site/language/jurisdiction logic; adding a
      plugin never requires touching this file again).
      Verify: `uv run ty check src` passes; `test_no_hardcoded_jurisdiction_or_language_literals_in_core`;
      `test_core_has_no_direct_llm_sdk_import`.
- [ ] `src/rtbf_agent/core/models.py`: `Case` and `Event` SQLModel tables per spec §3.2 — fields
      exactly as listed (`id, site, domain, target_email, state, applicable_jurisdiction,
      drafting_language, discovered_at, discovery_evidence, updated_at,
      reminder_enabled, reminder_interval_days`), kanban `state` as an enum with the 10
      states. `Event` insert-only at the repository layer, `detail` schema for document-related
      event kinds restricted to `{hash, size, extension?}`. `applicable_jurisdiction` and
      `drafting_language` are per-`Case` (nullable → falls back to global default, not derived
      from each other); `reminder_enabled`/`reminder_interval_days` likewise override the
      global reminder default per case.
      Discharges: R4, R5, R9 (state model supports indefinite waiting with no timeout field, and
      per-case reminder override), R22 (jurisdiction/drafting language live on `Case`, not just
      global config).
      Verify: `test_case_transitions_produce_event` (creates a `Case`, transitions `state`
      through all 10 states, asserts an `Event` row exists per transition);
      `test_event_table_has_no_update_or_delete_path`;
      `test_document_event_detail_schema_excludes_raw_content`;
      `test_case_level_reminder_config_overrides_global_default`;
      `test_case_level_drafting_language_and_jurisdiction_override_global_defaults_independently`.

## Phase 1 — mocked agent loop (proves the shape works end-to-end)

- [x] `src/rtbf_agent/plugins/llm/mock.py`: `MockLLMProvider` implementing `LLMProvider` —
      returns canned responses, no network calls.
      Discharges: R16, R17 (one adapter of the required set).
      Verify: `test_mockllmprovider_implements_llmprovider_contract`.
- [x] `src/rtbf_agent/plugins/notifiers/console.py`: `ConsoleNotifier` implementing `Notifier` —
      prints approval prompts to stdout and reads y/n from stdin, for local testing without a
      real Telegram bot.
      Discharges: R1 (send/attach gated on an explicit approval event, sourced here from a real
      human at the console, not auto-approved).
      Verify: `test_consolenotifier_implements_notifier_contract`.
- [x] `src/rtbf_agent/agent_loop.py`: orchestrator running discover → classify → draft → followup
      → notify for one mock `Case`, using the mock provider and console notifier.
      Discharges: R1, R3 (proposal events recorded even before approval), R9 (no auto-transition
      out of waiting states).
      Verify: `tests/test_agent_loop_mock.py` — a full cycle ends in `Borrador listo` state
      awaiting approval and never auto-advances past it;
      `test_send_without_approval_event_raises`;
      `test_case_in_waiting_state_never_auto_transitions_without_event`.
      **Known limitation, tracked for Phase 2 (see below):** `agent_loop.py` uses the synchronous
      `sqlmodel.Session` inside `async def` functions (`session.add`/`session.flush` block the
      event loop). Fine for the mocked Phase 1 loop; flagged twice by OpenCodeReview on PR #28 as
      needing a real fix before real connectors (Gmail/Telegram/real LLM adapters, all of which
      involve genuine concurrent I/O) are wired into this loop.
- [x] Raise `--cov-fail-under` in `.github/workflows/ci.yml` from 0 to a real threshold (suggest
      80) once Phase 0/1 land.
      Discharges: R27.
      Verify: manual — confirm the CI config value changed and a subsequent CI run enforces it.
      Set to 90 (actual coverage at the time was 96%; 90 is a tighter floor than the originally
      suggested 80 while still leaving headroom for Phase 2 modules with harder-to-cover I/O paths
      landing before their tests are fully fleshed out within the same PR).
- [x] CI check for R26 (every `core/`/`plugins/` module has a test file) added as a pytest/CI
      step, not just a review habit.
      Discharges: R26.
      Verify: `test_every_core_and_plugin_module_has_a_test_file` fails deliberately (temp file
      with no test) then passes once the missing test is added, before landing in CI.
      Landed in PR #25 (`tests/test_module_test_coverage.py`, merged 2026-07-12, closes #12);
      this checkbox was left unchecked in that PR — flipped here to match reality.

## Phase 2 — real connectors

- [x] Gmail discovery source (OAuth, read-only scope).
      Discharges: R21 (proves a discovery plugin can be added without touching `core/`).
- [ ] Migrate `agent_loop.py` from synchronous `sqlmodel.Session` to
      `sqlmodel.ext.asyncio.session.AsyncSession` (or run the sync session in an executor via
      `asyncio.get_running_loop().run_in_executor`). The mocked Phase 1 loop's `async def`
      functions call synchronous `session.add`/`session.flush` directly, which blocks the event
      loop — harmless with no real concurrent I/O yet, but the first real connector below
      (Gmail, then Telegram, then a real LLM adapter) introduces genuine concurrent I/O this loop
      needs to interleave with. Do this migration before wiring any real connector into
      `agent_loop.py`, not after — flagged twice by OpenCodeReview on PR #28.
      Verify: existing `tests/test_agent_loop_mock.py` still passes against the async session;
      add a test that two `run_cycle()` calls for different `Case`s can interleave (e.g. via
      `asyncio.gather`) without one blocking the other on DB I/O.
- [x] Telegram bot notifier (`python-telegram-bot`, whitelist by `chat_id` from the first
      message, `InlineKeyboardMarkup` for approve/reject — spec §4 requires buttons over free-text
      commands for critical decisions, to reduce mis-approval risk).
      Discharges: R1 (approval captured as an unambiguous button press, not free text), R15, R23
      (bot copy sourced from i18n resources, not literals).
      Verify: `test_first_ever_update_from_nonwhitelisted_chat_is_ignored`;
      `test_bot_sends_no_reply_to_nonwhitelisted_chat`;
      `test_no_literal_user_facing_strings_in_bot_or_kanban_code`;
      `test_approve_reject_decisions_use_inline_keyboard_not_free_text_parsing`.
      Landed in `src/rtbf_agent/plugins/notifiers/telegram.py` (issue #14): `TelegramNotifier`
      depends only on a narrow `TelegramBotClient` Protocol (test/DI seam, mirroring
      `plugins/senders/smtp.py`/`plugins/discovery/gmail.py`); `python-telegram-bot` itself is
      imported lazily inside `PythonTelegramBotClient`/`build_application()` only. Whitelist
      enforcement mechanism: `build_application()` registers `notifier.handle_update` as the
      *only* handler (one `TypeHandler(Update, ...)` matching every update type) on the real
      `Application`, and `handle_update()`'s first act, before any other branching, is comparing
      the update's chat id against the `chat_id` fixed at construction — no bootstrap window.
      `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` (`plugins/notifiers/config.py`) and
      `INTERFACE_LANGUAGE` (`rtbf_agent/config.py`'s `resolve_interface_language()`) are now real,
      read config surfaces — `.env.example`'s "not yet read by any code" markers for both removed.
- [x] First real jurisdiction plugin (GDPR) + first real language plugin (en), added as
      independent axes (`src/rtbf_agent/plugins/legal_templates/`: `jurisdictions/gdpr.py` holds
      legal-basis content only, `languages/en.py` holds phrasing only, `provider.py` is the only
      file where the two meet).
      Discharges: R22, R24 (jurisdiction and drafting language are independent keys).
      Verify: `test_all_combinations_of_interface_drafting_jurisdiction_are_valid`;
      `test_template_lookup_takes_jurisdiction_and_language_as_independent_params`;
      `test_new_drafting_language_added_without_touching_existing_jurisdiction_templates`.
- [x] Anthropic + OpenAI + OpenRouter + opencode + generic OpenAI-compatible adapters
      implementing `LLMProvider` — collapsed to two protocol adapters (`AnthropicAdapter`,
      `OpenAICompatibleAdapter`, `src/rtbf_agent/plugins/llm/`) plus a data-only provider registry
      (`registry.py`) and a 2-step env-then-local-config-file resolver (`config.py`), per
      `docs/architecture/llm-provider-patterns.md`.
      Discharges: R17, R18, R19.
      Verify: one `test_<provider>_adapter_implements_llmprovider_contract` per adapter
      (`tests/plugins/llm/test_anthropic.py`,
      `tests/plugins/llm/test_openai_compatible.py`);
      `test_localhost_configured_provider_makes_no_external_network_calls`
      (`tests/plugins/llm/test_network_isolation.py`);
      `test_classify_prompt_excludes_email_body_and_pii_fields` scoped down to what's testable at
      this layer since `agent_loop.py`/a classify call site don't exist yet (issue #10, not this
      issue's scope) — see `test_complete_only_sends_fields_present_on_the_request` in each
      adapter's test file, proving the adapter sends only what's on the `LLMRequest` it's given;
      data-sent accounting documented in `docs/llm-data-sent.md`.
- [ ] Sensitive document lifecycle: Telegram upload → encrypted temp storage → attach-and-secure-
      delete on approval → TTL purge if never approved.
      Discharges: R2, R6, R7, R8, R10.
      Verify: `test_uploaded_document_never_appears_plaintext_on_disk`;
      `test_document_secure_deleted_after_send`; `test_document_purged_after_ttl_expiry`;
      `test_ttl_default_is_24_to_48h`; README documents the Telegram-transit limitation (R10,
      manual verification).
- [ ] Kanban web UI (FastAPI backend already scaffolded; pick a frontend approach).
      Discharges: R23 (kanban strings from i18n too).
- [ ] `DEFAULT_CONFIG_PATH = Path.home() / ...` is evaluated at *module-import time* in all three
      config resolvers (`rtbf_agent/config.py`, `plugins/llm/config.py`,
      `plugins/senders/config.py`). In an environment with no `HOME` set (minimal containers, cron
      jobs, some CI runners), `Path.home()` raises `RuntimeError` on import — even if the operator
      set `RTBF_AGENT_CONFIG_PATH` to an absolute path and never needed the default at all.
      Flagged by OpenCodeReview on PR #37; deliberately deferred rather than fixed there, since a
      real fix means making `DEFAULT_CONFIG_PATH` lazy (e.g. a cached function) instead of a
      module-level constant across all three modules, which would also require restructuring the
      `monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")`
      test-isolation pattern used identically in all three test files. Low priority given this
      project's actual deployment model (one person running their own agent on their own machine,
      where `HOME` is essentially always set) — revisit if that assumption ever stops holding (e.g.
      packaging this to run under a minimal container or systemd unit without `HOME` inherited).

## Phase 3 — Automated case loop as the only driver (kanban + CLI + Telegram unified decision layer)

Full design: see the planning-session record (recurring background loop is the only thing that
ever advances a case; kanban/CLI/Telegram all record decisions through one shared layer; nothing in
the pipeline ever blocks waiting for a human). Real report: after a manual send, a case sat at
`Sent` forever instead of `Awaiting response`, and there was no way to run this unattended.

- [x] Phase 0: new `src/rtbf_agent/case_actions.py` — shared decision functions
      (`confirm_recipient`, `approve_draft`/`reject_draft`, `confirm_followup`/`dispute_followup`,
      `provide_text_response`/`attach_document`) + a plain exception vocabulary
      (`CaseNotFoundError`/`InvalidCaseStateError`/`InvalidInputError`). Refactor `api/approval.py`/
      `api/recipient.py` to delegate to it (no behavior change, same routes/status codes/tests).
- [x] Phase 1: new CLI subcommands (`cases`, `show`, `confirm-recipient`, `approve-draft`/
      `reject-draft`, `confirm-followup`/`dispute-followup`, `provide-text`/`attach-document`) —
      thin `case_actions` wrappers with deterministic exit codes and precise stderr error text.
- [x] Phase 2: remove every blocking path now that non-blocking replacements exist —
      `cli.prompt_recipient_email()`, `send_request()`'s/`confirm_followup_outcome()`'s
      `notifier.request_approval()` calls, `followup.provide_additional_info()`, and
      `Notifier.request_approval()`/`receive_document()`/`receive_text_response()`/
      `download_document_bytes()` (interface + `ConsoleNotifier`/`TelegramNotifier`
      implementations) all deleted. `Notifier` keeps only `notify()`. `CaseState.SENT` removed --
      a successful send moves straight to `Awaiting response`.
- [x] Phase 3: wire follow-up reply-checking into the Gmail-free `process` path without ever
      triggering an interactive OAuth handshake from an unattended process. New
      `_load_cached_gmail_credentials()`/`_gmail_credentials_available_noninteractively()`;
      `process_main_async()` builds real `reply_monitor`/`response_classifier` when a cached token
      is usable, otherwise skips follow-up for that pass (R9).
- [x] Phase 4: new `serve` subcommand — the recurring loop, interval configurable via
      `RTBF_SERVE_INTERVAL_MINUTES`/`--interval-minutes` (default 20 min). README's "Running a
      real case" section leads with it.
- [x] Phase 5: new `api/followup.py` (`POST /cases/{id}/followup/confirm`/`dispute`) and
      `api/document.py` (`POST /cases/{id}/document`/`text-response`), a new
      `followup.check_for_pending_additional_info()` polling step wired into `run_once()`, and the
      matching `case_detail.html` sections (pending-follow-up-confirmation, `Requires human
      action` text/document upload forms).
- [x] Phase 6 (issue #117): fixed the race where selecting a document or providing text alone
      could trigger a resend mid-round, before a human finished typing the note the LLM would
      draft from. New `case_actions.approve_additional_info_response()` (records
      `additional_info_ready`, requires a fresh document/text answer on record);
      `check_for_pending_additional_info()`'s gate now requires that fresh ready event, not just
      "documents or text present"; `resend_with_additional_info()` now also records its own
      `approval_granted` event and sends in the same pass (the ready action already is the human's
      approval — no separate `Draft ready`-column click). New route
      `POST /cases/{id}/additional-info/ready`, new CLI subcommand `approve-additional-info`, new
      case-detail button. PR #119.
      - Deferred follow-up (OCR review, non-blocking, not worth a rushed fix): `case_actions.
        _has_fresh_text_response()` duplicates `followup._pending_text_response()`'s staleness
        shape rather than importing it (matches this module's existing `_has_unconsumed_
        approval_event()` precedent) — the docstring should note explicitly that the two must be
        kept in sync if the staleness rule ever changes, not just cross-reference by name.
        `followup._has_fresh_additional_info_ready()` also mixes `sorted(...)[-1]` and `max(...)`
        for "find the latest event of a kind" in the same function — pick one and use it
        consistently (`sorted()[-1]`, matching most of this file's other helpers). Both are
        cosmetic/consistency-only, not correctness bugs; pick up next time either function is
        touched rather than as a standalone PR. **Superseded by Phase 7 below** — both functions,
        and the module they lived in the same shape of, are retired.
- [x] Phase 7 (maintainer decision after live-testing Phase 6/#119): retired Phase 6's fused-
      approval/auto-poll mechanism entirely — clicking "ready" composed a reply and sent it in the
      same breath, with zero review window, and getting a review window at all required a
      `Draft ready` kanban hop first. Replaced with an explicit **"Draft" preview action**:
      `followup.draft_additional_info_response()` -> `agent_loop.compose_additional_info_draft()`
      composes a `draft_proposed` event without ever moving `case.state` off `Requires human
      action`. Removed: `followup.check_for_pending_additional_info()`/
      `_has_fresh_additional_info_ready()`, `case_actions.approve_additional_info_response()`, the
      `additional_info_ready` event kind, `run_once()`'s additional-info pass, the
      `approve-additional-info` CLI subcommand/shell command, `POST .../additional-info/ready`, and
      the "ready" case-detail button. Added: `POST .../additional-info/draft` (`api/document.py`,
      needs a real `LLMProvider` — new `api/deps.py` `get_llm_provider()`/`get_llm_model()`
      dependencies, and a shared `plugins.llm.config.build_llm_provider()` factory `cli.py` now
      also uses instead of its own private copy), `draft-additional-info` CLI subcommand/shell
      command, a "Draft" case-detail button. Widened `case_actions._send_eligible_case()` (new
      shared gate replacing `_draft_ready_case()` at the three call sites that need it) so
      `approve_draft()`/`reject_draft()`/`edit_draft()` also accept "`Requires human action` + a
      pending draft" as an origin, not just `Draft ready` — `approve_draft()` is the one place
      `case.state` still moves for this path (flips to `Draft ready` right before the existing,
      unmodified send loop picks it up). `case_detail.py`'s `can_edit_draft`/`can_approve` widened
      to the same disjunction, so the same review/edit/approve/reject UI a `Draft ready` case gets
      renders automatically for a `Requires human action` case with a pending draft, without the
      case ever appearing to change kanban column.

## Manual-verification-only restrictions (no code path to test)

Tracked here so they aren't lost, per `REQUIREMENTS_TRACE.md`'s coverage check — checked at PR
review time, not by a test:
- R10, R11 — third-party infra limits documented in README, no absolute-deletion claims anywhere
  in user-facing copy.
- R12, R13 — architecture review: no author-operated backend, no telemetry dependency.
- R14 — secrets never committed; confirmed via `.gitignore` coverage + secret scanning before
  merge to `main`.
- R25 — README states "own data only, not for third parties without consent."
- R28 — docs updated in the same PR as the code they describe.
- R29 — this file and, later, `ROADMAP.md`/`MILESTONES.md` stay the source of phased scope.
- R30 — clean commit history on `main`, no accumulated "wip" commits.

## Not scoped yet

- [x] `CONTRIBUTING.md`, `docs/i18n/` translation structure, `ROADMAP.md` — write once Phase 1
      proves the architecture, not before (avoid documenting an interface that Phase 1 forces you
      to change). Phase 0/1 (`#6`-`#12`) merged; issue #20 closed this.
