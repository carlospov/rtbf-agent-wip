# Requirements trace — R1 through R30

Translates each numbered restriction in [`RESTRICTIONS.md`](RESTRICTIONS.md) into (a) a concrete,
testable functional requirement and (b) at least one test that proves it — or, where a test can't
practically cover it, a named manual-verification step and why. This is the missing link the
restrictions doc itself calls out ("Next step (not this session)"): it's what
[`tasks/todo.md`](../../tasks/todo.md) is now built from, checklist item by checklist item.

Traceability chain: spec (`docs/specs/2026-07-12-rtbf-agent-requirements.md`) → `RESTRICTIONS.md`
→ this file → `tasks/todo.md` → code + test. Every `R#` below cites the `OBJECTIVES.md` objective
(`O#`) its functional requirement belongs to.

---

## Human-in-the-loop / irreversibility (spec §2.1, §7)

### R1 — No irreversible action without send-time approval
**Restriction:** No irreversible action (sending a request, attaching a document, signing
anything) executes without explicit human approval at the moment of execution — approval given
earlier (e.g. at draft time) does not substitute for approval at send-time.
**Functional requirement:** `send_request(case_id)` and `attach_document(case_id, doc_id)` (or
their orchestrator equivalents) refuse to execute unless a matching approval `Event` exists for
that specific case and that specific action, recorded after the draft/document was finalized —
never inferred from an earlier "looks good" at draft review.
**Test:** `test_send_without_approval_event_raises`,
`test_draft_time_approval_does_not_authorize_later_send` (approve at draft, then attempt send with
no send-time approval event → still rejected). On the Telegram surface specifically (spec §4),
approval must be captured as an unambiguous button press (`InlineKeyboardMarkup`), not parsed from
free text, to minimize mis-approval risk:
`test_approve_reject_decisions_use_inline_keyboard_not_free_text_parsing`.
**Objective:** O6, O8, O9.

### R2 — Agent never resolves identity verification itself
**Restriction:** The agent never resolves identity verification on its own: it does not fill
identity forms, does not sign documents, does not generate or edit identification images.
**Functional requirement:** No function in `core/` or any built-in plugin accepts an identity
document and produces a modified/filled/signed version of it; the only permitted operations on a
document are: receive, store encrypted, attach-as-is, delete.
**Test:** `test_no_document_mutation_api_exists` (static check: no function signature in
`core/`/`plugins/` takes a document and returns a document). **Manual verification:** code review
confirms no LLM call is ever given a document image as input for anything other than optional
OCR-for-classification (if added later), never for form-filling.
**Objective:** O12.

### R3 — Every proposal is recorded, not just executed ones
**Restriction:** Every proposal the agent makes — including ones the human does not approve — is
recorded, not just the ones that execute.
**Functional requirement:** When the agent produces a draft, a classification, or a followup
interpretation, a `Event` of kind `*_proposed` is written before any approval decision is known;
rejection writes a further `Event`, it never deletes or overwrites the proposal event.
**Test:** `test_rejected_draft_still_has_proposed_event`,
`test_proposal_event_exists_before_approval_decision_event`.
**Objective:** O1, O9.

## Audit & logging (spec §2.2, §3.2)

### R4 — Append-only, immutable log
**Restriction:** Every agent action is recorded in an append-only, immutable log with actor,
timestamp, and detail.
**Functional requirement:** `Event` rows are insert-only — no code path in `core/` updates or
deletes an `Event` row after creation; every `Event` has non-null `actor`, `timestamp`, `detail`.
**Test:** `test_event_table_has_no_update_or_delete_path` (assert no such method exists on the
repository/model layer; attempting a raw update in a test still succeeds at the DB level but no
application code path can trigger it — covered by a static/import check plus a "no such method"
assertion), `test_every_event_has_actor_timestamp_detail`.
**Objective:** O1.

### R5 — No document content in audit log
**Restriction:** Audit log entries involving sensitive documents never contain document content —
only a hash and metadata (e.g. "attached", "sent").
**Functional requirement:** `Event.detail` for `document_received`/`document_attached`/
`document_sent`/`document_purged` is restricted to `{hash, size, extension?}` — the serializer for
these event kinds has no field capable of holding raw bytes or extracted text.
**Test:** `test_document_event_detail_schema_excludes_raw_content`,
`test_document_event_never_contains_bytes_or_extracted_text` (fuzz: pass full document bytes into
the event-writing path, assert they never land in the persisted row).
**Objective:** O1, O12.

## Sensitive data minimization (spec §2.3, §3.3, §3.4)

### R6 — No plaintext-on-disk for sensitive documents
**Restriction:** Sensitive documents are never written to disk in plaintext — held in memory or in
encrypted temporary storage only.
**Functional requirement:** The document-intake path writes to disk only through an
encryption-at-write wrapper; there is no code path that calls a plain `open(..., "wb")`/
`Path.write_bytes` on document content.
**Test:** `test_uploaded_document_never_appears_plaintext_on_disk` (upload a document with a unique
byte marker in a temp dir, scan every file under the configured storage root for that marker in
the clear, assert zero matches).
**Objective:** O12.

### R7 — Secure delete after send
**Restriction:** After being attached and sent, a sensitive document is deleted via secure delete
(overwrite), not merely unlinked/logically deleted.
**Functional requirement:** The post-send cleanup path overwrites the document's storage location
with random/zero bytes before removing the file/entry; a "logical delete only" (flag flip, plain
unlink) never satisfies this path.
**Test:** `test_document_secure_deleted_after_send` (capture the file's disk offset/inode before
deletion where feasible, or assert the storage backend's `delete` implementation always calls
`overwrite_then_unlink`, never bare `unlink`).
**Objective:** O12.

### R8 — TTL purge for unapproved documents
**Restriction:** A sensitive document not approved for sending within a configurable TTL (default
24-48h) is purged automatically; the user is asked to re-upload if still needed.
**Functional requirement:** Each stored document has a `received_at` and a configurable
`ttl_hours` (default within [24, 48]); a background sweep purges (securely, per R7) any document
past `received_at + ttl_hours` still lacking a send-approval `Event`, and triggers a
re-upload-request notification.
**Test:** `test_document_purged_after_ttl_expiry`, `test_ttl_default_is_24_to_48h`,
`test_purge_triggers_reupload_notification`.
**Objective:** O12.

### R9 — No timeout/auto-escalation on waiting cases
**Restriction:** A `Case` in a waiting state has no timeout, auto-close, or auto-escalation. The
only permitted automated behavior is an opt-in, off-by-default "N days without updates" notice
that never itself changes `Case` state or triggers any resend.
**Functional requirement:** No scheduled/background job calls a `Case` state-transition function
based on elapsed time; the reminder feature (opt-in, default off) only calls the notifier, never
`update_case_state` or `send_request`. Reminder config is resolved per-case first
(`Case.reminder_enabled`/`reminder_interval_days`, spec §3.2), falling back to a global default
when the case hasn't set its own — a case can opt in/out or pick its own `N days` independently of
the global setting (spec §3.3: "opcional y configurable por caso o globalmente").
**Test:** `test_case_in_waiting_state_never_auto_transitions_without_event` (advance a fake clock
arbitrarily far, assert state unchanged with no human/followup event), `test_reminder_default_off`,
`test_reminder_notice_calls_notifier_only_not_state_or_send`,
`test_case_level_reminder_config_overrides_global_default`,
`test_case_can_opt_out_of_reminder_when_global_default_is_on`.
**Objective:** O7.

## Platform boundaries / transparency (spec §2.4, §3.4.6, §7)

### R10 — Document third-party infrastructure limits
**Restriction:** The project does not claim control or guarantees over data once it has transited
third-party infrastructure (e.g. a file after passing through Telegram's Bot API) — this
limitation must be documented explicitly, not silently assumed.
**Functional requirement:** README (and in-bot copy, once i18n exists) states explicitly that once
a file transits Telegram's Bot API, its handling beyond that point is outside this project's
control.
**Manual verification:** reviewer confirms the sentence exists in `README.md` (and later, in the
relevant locale resource file) before merging any PR that ships the Telegram notifier. Not
testable in code — it's a documentation claim about a system this project doesn't control.
**Objective:** O12, O13.

### R11 — No deletion guarantee on third-party infra
**Restriction:** No guarantee of deletion on third-party infrastructure (Telegram, email
providers) beyond what that platform itself allows.
**Functional requirement:** No user-facing copy (README, bot messages, kanban UI) asserts or
implies that this project can guarantee deletion on Telegram/email-provider infrastructure.
**Manual verification:** reviewer greps user-facing strings for absolute claims ("guaranteed",
"permanently deleted by us") before merge; same rationale as R10 — a claims-audit, not testable.
**Objective:** O13.

## Decentralization / privacy (spec §2.5, §2.6, §4, §7)

### R12 — No centralized backend
**Restriction:** No centralized backend or service operated by the project's author. Everything
runs locally using the user's own credentials.
**Functional requirement:** No code in the repo makes a network call to any domain owned/operated
by the project's author; all external calls are to (a) the user-configured LLM provider endpoint,
(b) the user's own Telegram bot via Telegram's public Bot API with the user's own token, (c) the
user's own email provider.
**Test:** `test_no_hardcoded_author_owned_domain_in_source` (grep source for outbound URLs, assert
none point to a project-controlled domain). **Manual verification:** architecture review confirms
no component requires a hosted service the author operates.
**Objective:** O13 (cross-cutting; enforced by review, not a single module).

### R13 — Zero telemetry
**Restriction:** Zero telemetry sent to the project's author.
**Functional requirement:** No code path sends usage data, analytics events, crash reports, or any
other telemetry to a project-author-controlled endpoint; no analytics SDK is a dependency.
**Test:** `test_no_telemetry_dependency_in_pyproject` (assert no known analytics/telemetry package
— e.g. sentry-sdk configured with a project DSN, segment, posthog — is a runtime dependency, or if
present is fully opt-in and off by default with no default endpoint set).
**Objective:** O13.

### R14 — Secrets never committed
**Restriction:** Secrets (Telegram bot token, LLM API keys, etc.) are never committed to the repo;
`.gitignore` covers them from the initial scaffold onward.
**Functional requirement:** `.env`/config files holding secrets are listed in `.gitignore`; no
example/config file in the repo contains a real-looking secret value (placeholders only).
**Test:** `test_gitignore_covers_env_and_secret_config_paths` (assert `.env`, `*.local.toml` or
whatever the chosen config format is, are present in `.gitignore`). **Manual verification:**
`git log -p` / secret-scan (e.g. `gh secret-scanning` or `trufflehog`) run before each merge to
`main` confirms nothing slipped through — already partially covered by CI's use of GitHub's own
secret scanning; document as a pre-merge check either way.
**Objective:** O13.

### R15 — chat_id whitelist from first message
**Restriction:** The Telegram bot enforces a `chat_id` whitelist starting from the very first
message — any non-whitelisted chat is ignored, not just chats after initial setup.
**Functional requirement:** Every incoming Telegram update (including the very first one the bot
process ever receives, before any "setup" state exists) is checked against the configured
`chat_id` allowlist before any handler logic runs; non-matching `chat_id`s are dropped silently
(no reply, to avoid confirming the bot's existence to strangers).
**Test:** `test_first_ever_update_from_nonwhitelisted_chat_is_ignored`,
`test_whitelisted_chat_id_is_processed`, `test_bot_sends_no_reply_to_nonwhitelisted_chat`.
**Objective:** O8.

## LLM provider abstraction (spec §0.1)

Design precedent and recommended shape for R16-R19 (single narrow interface, two protocol
adapters, data-only provider registry, layered config resolution):
[`docs/architecture/llm-provider-patterns.md`](../architecture/llm-provider-patterns.md).

### R16 — Core never imports a concrete LLM SDK directly
**Restriction:** Core code never imports a concrete LLM SDK (Anthropic, OpenAI, etc.) directly
outside of designated provider-adapter modules; all LLM calls go through an abstract provider
interface.
**Functional requirement:** No file under `src/rtbf_agent/core/` imports `anthropic`, `openai`, or
any other concrete LLM SDK; all such imports are confined to `src/rtbf_agent/plugins/llm/*`.
**Test:** `test_core_has_no_direct_llm_sdk_import` (AST-walk every file under `core/`, assert no
`import`/`from` statement names a known concrete LLM SDK package).
**Objective:** O3.

### R17 — Minimum supported providers at launch
**Restriction:** Minimum supported providers at launch: Claude (Anthropic API), OpenAI,
OpenRouter, opencode, and any OpenAI-API-compatible endpoint.
**Functional requirement:** `LLMProvider` has a concrete adapter for each of: Anthropic, OpenAI,
OpenRouter, opencode, and a generic OpenAI-compatible adapter (parametrized by base URL) covering
self-hosted endpoints (Ollama/LM Studio/vLLM).
**Test:** one contract test per adapter — `test_<provider>_adapter_implements_llmprovider_contract`
— run against a recorded fixture or a local fake server; `test_generic_openai_compatible_adapter_works_against_localhost`
using a stub HTTP server (proves the "no outbound internet required" claim end to end for this
adapter).
**Objective:** O3.

### R18 — Local-only config, works fully offline against localhost
**Restriction:** Provider configuration (API key, base URL, model) lives in local config/env only,
never in the repo. The system must work fully, including with zero outbound internet access, when
configured against a `localhost` endpoint.
**Functional requirement:** Provider config is read exclusively from env vars / a local
gitignored config file, never from a value hardcoded in source; when `base_url` points at
`localhost`/`127.0.0.1`, no code path attempts an outbound call to any non-local host.
**Test:** `test_provider_config_has_no_hardcoded_default_pointing_offsite`,
`test_localhost_configured_provider_makes_no_external_network_calls` (run with network access
blocked in the test sandbox, assert the classify/draft flow still completes against a local fake
LLM server).
**Objective:** O3.

### R19 — Minimum-necessary data sent to LLM, documented
**Restriction:** Only the minimum data necessary for the specific task (classify/draft/etc.) is
sent to the LLM, and what is/isn't sent must be documented clearly enough that a self-hosting,
privacy-conscious user can audit it without reading the source.
**Functional requirement:** Each LLM call site builds its prompt/payload from an explicit,
narrowly-scoped set of fields (e.g. classify sends domain + page metadata, never raw email body);
a docs page (`docs/llm-data.md` or README section) enumerates, per task, exactly what fields are
sent.
**Test:** `test_classify_prompt_excludes_email_body_and_pii_fields`,
`test_draft_prompt_includes_only_case_fields_needed_for_legal_template`. **Manual verification:**
docs review confirms the "what's sent" table matches the actual payload-building code (kept
honest by the same PR that changes either).
**Objective:** O3.

**Update (issue #34):** `_draft()` (`src/rtbf_agent/agent_loop.py`) sends *nothing* to the LLM at
all — the strictest possible reading of "minimum data necessary." The draft body is composed
deterministically from `LegalTemplateProvider`'s template plus plain `Case` fields
(`target_email`, `site`, `domain`) and the local `requester_name` config value
(`src/rtbf_agent/config.py`); no LLM call is in the drafting path, so there's no prompt payload to
audit for over-inclusion. `_classify()` is unaffected and still sends only `case.domain` to the
LLM, per the table above. See `_draft()`'s docstring for the full rationale (an LLM step can't be
reliably constrained to touch only surrounding prose without risking the verbatim `legal_basis`
citation).

## Extensibility / no hardcoding (spec §0 "regla de oro", §2.7)

### R20 — No site/language/jurisdiction logic in core
**Restriction:** No site-, language-, or jurisdiction-specific logic lives in `core/` — anything
that only makes sense for one language, country, or site must be a plugin/adapter.
**Functional requirement:** `core/` contains only interfaces, orchestration, and data-model code;
no `if domain == "..."`, no jurisdiction/legal-basis string literal, no hardcoded language string
appears under `core/`.
**Test:** `test_no_hardcoded_jurisdiction_or_language_literals_in_core` (grep `core/` for a
maintained list of known jurisdiction codes / ISO language codes appearing as literals outside
type annotations/docstrings; fails the build if found).
**Objective:** O2.

### R21 — New plugin never requires a core change
**Restriction:** Adding a new language, jurisdiction, email provider, or site type requires only
adding a new plugin module — never a change to core.
**Functional requirement:** Adding a new jurisdiction/language/discovery-source plugin is
achievable by adding one new module under `plugins/` that implements an existing `core/interfaces`
ABC and registering it in a plugin registry/entry-point — with zero diff lines under `core/`.
**Test:** `test_adding_a_stub_plugin_requires_no_core_changes` (integration test: register a
throwaway test plugin implementing each interface, assert it's discoverable/usable without
touching `core/`). **Manual verification:** when the first second-jurisdiction/language plugin is
actually added (Phase 2), confirm the PR diff touches no file under `core/`.
**Objective:** O2.

## Internationalization (spec §0, §6)

### R22 — Three independent i18n settings
**Restriction:** Interface language, drafting language, and legal jurisdiction are three
independently configurable settings; no code path assumes any two of them are coupled or
derivable from one another.
**Functional requirement:** `interface_language` is a global-only user setting (governs kanban +
bot chrome, not any single request). `drafting_language` and `jurisdiction` are **per-`Case`**
fields (spec §3.2: `Case.drafting_language`, `Case.applicable_jurisdiction`) — each `Case` can set
either independently of the other and independently of the global default, e.g. a user with
`interface_language=es` filing against a Japanese site gets `jurisdiction` inferred/set from that
site's country while `drafting_language` may be `ja` or `en`, none of which is derived from the
other two. A global default exists for convenience (used when a case doesn't override it) but no
code path treats the global default as authoritative once a case has its own value, and no
function derives `jurisdiction` from `drafting_language` or vice versa.
**Test:** `test_all_combinations_of_interface_drafting_jurisdiction_are_valid` (parametrized test
matrix over supported values, asserts no combination is rejected or silently coerced);
`test_case_level_drafting_language_and_jurisdiction_override_global_defaults_independently`;
`test_interface_language_has_no_per_case_override_field` (interface language is global-only, so
this isn't a `Case` column — guards against accidentally adding one and conflating the axes).
**Objective:** O11.

### R23 — Every interface string from a translation system
**Restriction:** Every interface string (kanban UI + bot messages) is sourced from a
translation/resource system — never a literal string embedded in code.
**Functional requirement:** All user-facing strings in the kanban and bot code are looked up via
the i18n resource mechanism (e.g. `t("case.state.discovered")`), never written as a literal in the
handler/view code.
**Test:** `test_no_literal_user_facing_strings_in_bot_or_kanban_code` (lint-style AST check
flagging string literals passed directly to `send_message`/response-render calls that aren't a
translation-key lookup).
**Objective:** O8, O10, O11.

### R24 — Legal templates parametrized on two independent axes
**Restriction:** Legal request templates are parametrized on two independent axes: jurisdiction
and drafting language — not a single combined axis.
**Functional requirement:** `LegalTemplateProvider` is looked up by `(jurisdiction,
drafting_language)` as two independent keys, not a single combined key like `"gdpr_es"`; a
template for a new drafting language on an existing jurisdiction requires adding only a
language-side resource, not a new template per jurisdiction.
**Test:** `test_template_lookup_takes_jurisdiction_and_language_as_independent_params`,
`test_new_drafting_language_added_without_touching_existing_jurisdiction_templates`.
**Objective:** O6, O11.

## Scope boundaries (spec §2.6, §7)

### R25 — README states own-data-only scope
**Restriction:** The system is built and licensed for a user managing their own data only. The
README must state explicitly that it is not intended for managing deletion of third parties' data
without their explicit consent.
**Manual verification:** reviewer confirms `README.md` contains an explicit "own data only, not
for third-party deletion without consent" statement before merging the first README that ships.
Not testable in code — it's a documentation/policy claim.
**Objective:** O13.

## Engineering standard (spec §0.1 "estándar de ingeniería")

### R26 — No new core/plugins module without tests
**Restriction:** No new module under `core/` or `plugins/` ships without tests: unit tests for
core logic, integration tests for full flows (with mocks for LLM, Telegram, and email).
**Functional requirement:** Every `.py` file added under `core/` or `plugins/` has a corresponding
test file under `tests/` in the same PR.
**Test:** `test_every_core_and_plugin_module_has_a_test_file` (CI check: for each
`core/**/*.py` / `plugins/**/*.py` module, assert a `tests/**/test_<module>*.py` exists).
**Objective:** O13.

### R27 — CI fails on lint failure or coverage regression
**Restriction:** CI fails the pipeline on lint failure or on a coverage regression below the
configured threshold.
**Functional requirement:** `.github/workflows/ci.yml` runs `ruff check`, `ruff format --check`,
`ty check`, and `pytest --cov-fail-under=<real threshold>`, all as required (non-continue-on-error)
steps; `--cov-fail-under` is a real number (not the current placeholder `0`) once Phase 0/1 land.
**Manual verification:** reviewer confirms the CI config still has all four required steps and
that `--cov-fail-under` was raised per `tasks/todo.md`'s Phase 1 item — a config-file check, not
something `pytest` itself can assert.
**Objective:** O13.

### R28 — Docs updated in the same PR as the code they describe
**Restriction:** Documentation is updated in the same PR as the code change it describes —
documentation drift is treated as a bug.
**Manual verification:** PR review checklist item — reviewer confirms README/architecture
docs/config guides changed in the same diff whenever the code they describe changed. Inherently a
process rule, not something a test can enforce.
**Objective:** O13.

### R29 — Explicit phased roadmap with verifiable done-criteria
**Restriction:** Development follows an explicit, phased roadmap with a verifiable "done"
criterion per phase, to prevent scope creep.
**Manual verification:** confirmed by the existence and upkeep of `tasks/todo.md` (this
translation) and, later, `ROADMAP.md`/`MILESTONES.md` (per `OBJECTIVES.md` O13) — each phase has a
named verification step. Reviewed at the start of each new phase, not testable in code.
**Objective:** O13.

### R30 — Clean commit/PR history
**Restriction:** Commit/PR history uses a clean, conventional-commit-style structure — no
accumulated "wip" commits left on `main`.
**Manual verification:** PR review checklist item — reviewer confirms no "wip"/"fixup" commits
land on `main` (squash or rebase before merge if needed). Process rule, not testable.
**Objective:** O13.

---

## Coverage check

All 30 restrictions (`R1`-`R30`) from `RESTRICTIONS.md` are addressed above: R1-R9, R15, R16-R24,
R26-R27 have concrete automated tests named; R2 has both a static check and a manual review step;
R10-R14, R25, R28-R30 are manual-verification-only, each with a stated reason a test can't cover
it (documentation claims, process rules, or architecture-wide review rather than a single code
path).
