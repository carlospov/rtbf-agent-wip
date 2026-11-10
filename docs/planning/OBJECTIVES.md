# Objectives

High-level objectives for RTBF-Agent, one per functional area. Each has "Open tasks" — outcomes
that must be true, deliberately **not** prescribing classes, file paths, or libraries. Concrete
implementation choices are made later, task by task, once each objective's restrictions (see
[`RESTRICTIONS.md`](RESTRICTIONS.md)) have been translated into functional requirements and tests.

Source: [`docs/specs/2026-07-12-rtbf-agent-requirements.md`](../specs/2026-07-12-rtbf-agent-requirements.md)
(section 3.1 for the module list; sections 0/0.1/2 for the cross-cutting objectives below).

---

## O1. Core data model & audit trail

Foundational — almost every other objective depends on this existing first.

**Open tasks:**
- A `Case` representation that captures everything in spec §3.2 and can move through all 10 kanban
  states (`Discovered → ... → Closed`).
- An `Event` representation that is provably append-only and immutable (once written, an event
  cannot be edited or deleted by any code path).
- A way to prove, in a test, that every `Case` state transition produces a corresponding `Event`.
- A way to prove that sensitive-document events never contain document content — only hash and
  metadata (ties to R5).

**Depends on:** nothing.

---

## O2. Plugin & extensibility framework

**Open tasks:**
- A documented interface (contract, not implementation) for each plugin family: discovery source,
  legal/jurisdiction template provider, language/translation resource, notifier.
- A way to add a new instance of any of these four families without modifying anything outside the
  new plugin's own module.
- A test or check that fails if code outside `core/`'s interface definitions and a plugin's own
  module references site-, language-, or jurisdiction-specific logic (ties to R20–R21).

**Depends on:** O1.

---

## O3. LLM provider abstraction

**Open tasks:**
- An abstract provider contract that classification/drafting/reasoning code depends on exclusively.
- At least Anthropic, OpenAI, OpenRouter, opencode, and a generic OpenAI-compatible endpoint
  (covering self-hosted Ollama/LM Studio/vLLM) usable through that contract without touching core.
- Provider configuration (key, base URL, model) resolved from local config/env only, never from
  repo-committed values, and functional with no outbound internet access when pointed at
  `localhost`.
- Clear documentation of exactly what data is sent to the LLM for each task and what isn't.

**Depends on:** O2.

---

## O4. Discovery

**Open tasks:**
- At least one real (not fabricated/mocked-only) way to detect that an account exists at a given
  site, tied to the user's own email.
- The detection mechanism is swappable/extensible as a plugin under the O2 interface, not the only
  possible source forever.
- Evidence of how each account was discovered is captured on the `Case` (spec §3.2
  `discovery_evidence`).

**Depends on:** O1, O2.

---

## O5. Classifier

**Open tasks:**
- Given a discovered domain, determine its privacy contact (DPO/privacy@) and whether a
  self-service deletion flow exists.
- Classification result is enough to unblock drafting without further manual research in the
  common case.

**Depends on:** O1, O2, O3 (if classification uses the LLM).

---

## O6. Drafting

**Open tasks:**
- Produce a deletion request, in the configured drafting language, that cites the legal basis
  applicable to the site's jurisdiction.
- Drafting language and legal jurisdiction are handled as two independent parameters — a request
  can be drafted in any supported language citing any supported jurisdiction's basis, in any
  combination (ties to R24).
- A drafted request always lands in a state that requires human approval before anything is sent
  (ties to R1).

**Depends on:** O1, O2, O3.

---

## O7. Followup

**Open tasks:**
- Interpret an incoming response as one of: confirmation, rejection, request for additional
  verification, or silence — and move the `Case` to the matching state.
- A `Case` can sit indefinitely in a waiting state with zero automatic side effects (ties to R9).
- The only automated notice possible is an opt-in, off-by-default "N days without updates" message
  that never changes `Case` state or triggers a resend on its own.

**Depends on:** O1, O2, O3.

---

## O8. Notifier / Telegram

**Open tasks:**
- A generic notifier contract that the agent loop and other modules depend on, not a concrete
  channel.
- A Telegram implementation of that contract: delivers notifications, collects approve/reject
  decisions, and receives documents associated with a specific `Case`.
- Whitelist enforcement by `chat_id` from the first message onward (ties to R15).
- Messages/commands rendered in the user's configured interface language (ties to R23).

**Depends on:** O1, O2, O11.

---

## O9. Agent loop (orchestration)

**Open tasks:**
- Runs the discover → classify → draft → followup → notify cycle persistently, unattended, for as
  long as the user leaves it running.
- Provably never advances a `Case` past a human-approval gate without a recorded approval `Event`.
- Survives being restarted mid-cycle without losing or duplicating a `Case`'s state.

**Depends on:** O1 through O8 all being usable behind their interfaces (concrete implementations
can start as mocks/stubs — see restriction-to-test translation, next planning session).

---

## O10. Kanban web

**Open tasks:**
- A browsable view of every `Case` grouped by kanban state, and a chronological audit view per
  `Case` (its `Event` history).
- Reads from the same local data store as Telegram — no separate source of truth, no drift between
  the two surfaces.
- Interface language configurable independently of drafting language/jurisdiction (ties to R22).

**Depends on:** O1, O11.

---

## O11. Internationalization

**Open tasks:**
- Interface language, drafting language, and legal jurisdiction exist as three settings a user can
  set independently, with no code path that assumes any two of them are coupled.
- Every interface string (kanban + bot) is sourced from a translation/resource mechanism, not
  embedded as a literal in code.
- Legal templates are parametrized on two independent axes: jurisdiction and drafting language.

**Depends on:** O2.

---

## O12. Security & sensitive-data handling

**Open tasks:**
- The full identity-document lifecycle is real: upload (via Telegram) → encrypted temporary
  storage (never plaintext disk) → marked ready-to-attach → attached and securely deleted
  immediately on approval → purged on TTL expiry if never approved (default 24–48h, configurable).
- A way to verify, concretely (not just by code review), that no sensitive document content ever
  reaches disk unencrypted or reaches the audit log.
- README-level documentation that once a file transits Telegram's infrastructure, its handling
  beyond that point isn't under this project's control (ties to R10).

**Depends on:** O1, O8.

---

## O13. Engineering & OSS standard

Largely scaffolded already (CI, licensing, code-reviewer subagent); this objective tracks what's
left as the codebase grows.

**Open tasks:**
- Coverage gate in CI raised from its current placeholder (0) to a real, enforced threshold once
  enough real code exists to make that meaningful.
- `CONTRIBUTING.md` and a phased `ROADMAP.md`/`MILESTONES.md` with a verifiable done-criterion per
  phase, so scope per phase stays explicit.
- Docs (README, architecture notes, config guides) stay updated in the same PR as the code changes
  they describe — treated as a bug if they drift.
- `docs/i18n/` structure ready to receive community documentation translations (English stays the
  base language for docs).

**Depends on:** ongoing, no hard blocker.
