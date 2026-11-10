# Contributing to RTBF-Agent

This project is the public showcase for `outlaw-labs` — contributions are held to that bar from
the first commit, not just once something "works." Thanks for reading this before opening a PR.

> Just want to *run* this, not contribute code? See [`docs/user-guide/`](docs/user-guide/README.md)
> instead — it covers setup (LLM provider, Gmail OAuth, Telegram, SMTP) and running a real case.

## Before you start

Read, in this order:
1. [`README.md`](README.md) — what this project is and its non-negotiable principles.
2. [`CLAUDE.md`](CLAUDE.md) — repo conventions, commands, and the workflow every change follows.
3. [`docs/specs/2026-07-12-rtbf-agent-requirements.md`](docs/specs/2026-07-12-rtbf-agent-requirements.md)
   — the full requirements spec (written in Spanish; it's the original business/legal
   specification and is not translated — see [`docs/i18n/README.md`](docs/i18n/README.md) for what
   *is* in scope for community translation).
4. [`docs/planning/OBJECTIVES.md`](docs/planning/OBJECTIVES.md) and
   [`docs/planning/RESTRICTIONS.md`](docs/planning/RESTRICTIONS.md) — the "why" behind every
   constraint, and the numbered restrictions (`R1`-`R30`) every change is checked against.
5. [`docs/planning/REQUIREMENTS_TRACE.md`](docs/planning/REQUIREMENTS_TRACE.md) — how each
   restriction maps to a concrete functional requirement and a test.
6. [`tasks/todo.md`](tasks/todo.md) and [`ROADMAP.md`](ROADMAP.md) — what's done, what's in
   progress, what's next.

## Non-negotiables

These are not style preferences — a PR that violates one of these will be rejected regardless of
how well-tested or well-written it otherwise is:

- **No irreversible action without human approval at send-time.** Never auto-send a request,
  auto-attach a document, or auto-sign anything. See `agent_loop.py`'s `_execute_send()` for the
  pattern every new irreversible action must follow: a guard that refuses to run without an
  already-persisted approval `Event`, checked immediately before the action, not "trust the
  caller already checked."
- **No hardcoded language/jurisdiction/site logic in `core/`.** That belongs in `plugins/`, behind
  an interface in `core/interfaces.py`. Adding a new jurisdiction, language, discovery source, LLM
  provider, or notification channel must never require editing `core/interfaces.py` — if it does,
  the abstraction is wrong.
- **LLM calls go through `LLMProvider`, never a concrete SDK directly.** Must work identically
  against Anthropic, OpenAI, OpenRouter, or a local OpenAI-compatible endpoint (Ollama, vLLM...).
- **No sensitive data in plaintext at rest.** Identity documents: encrypted temp storage, short
  TTL, secure delete after use. Audit log (`Event`): hash + metadata only, never content.
- **Every new module in `core/` or `plugins/` ships with tests.** Not "added later" — this is
  enforced by CI (`tests/test_module_test_coverage.py`, R26), not just review habit.
- **Code identifiers are English-only, even though the requirements spec is written in Spanish.**
  `docs/specs/2026-07-12-rtbf-agent-requirements.md` stays in Spanish — it's the original
  business/legal specification, not code — but every field name, enum value, function name, and
  variable name must be in English regardless of the term the spec uses for it. Translate the
  *concept*, never copy the *term*. This already happened once (`core/models.py`'s field names and
  `CaseState` values were originally lifted verbatim from the spec's Spanish wording) and had to be
  renamed after the fact.

## Development setup

```bash
uv sync --all-extras --dev
cp .env.example .env   # fill in your own credentials — never commit this file
uv run pytest
```

## Workflow for a change

1. **Non-trivial change → plan first.** Update [`tasks/todo.md`](tasks/todo.md) with what you're
   about to do and why, before writing code — don't just start editing.
2. **One commit per completed checklist item.** Makes an easy revert if something's wrong, and
   keeps `git log` readable (R30: no accumulated "wip" commits on `main`).
3. **Before claiming a change works, actually run it:**
   ```bash
   uv run pytest
   uv run ruff check .
   uv run ruff format .
   uv run ty check src
   ```
   Don't assert something passes without having run it.
4. **Every module ships tests in the same PR**, not a follow-up. `tests/test_module_test_coverage.py`
   will fail CI if a new `core/`/`plugins/` module has no matching test file.
5. **Docs update in the same PR as the code they describe** (R28) — a behavior change without a
   docs update in the same diff will be asked to add one, not merged as-is.

## Code review

Every PR touching `src/` goes through two independent review passes before merge:

- **This repo's own `code-reviewer` subagent/checklist** — checks specifically for the
  non-negotiables above (human-in-the-loop gating, no hardcoded jurisdiction/language logic,
  secrets handling, LLM abstraction).
- **[OpenCodeReview](https://github.com/alibaba/open-code-review)**, wired into CI
  (`.github/workflows/code-review.yml`) — general-purpose defect-finding (bugs, security, style)
  as a non-blocking comment bot. It complements the checklist above, not a replacement — it
  doesn't know this repo's specific restrictions.

Take findings from both seriously, but use judgment: verify a finding against the actual code
before applying a suggested fix, and it's fine to skip a low-value/false-positive suggestion as
long as the reasoning is documented in the commit message (this repo's history has several
examples — e.g. a suggested fix that would have broken this project's specific type checker's
narrowing rules was skipped, with the verification steps documented in the commit).

## Adding a new plugin

Every plugin category has one ABC in `core/interfaces.py` and at least one concrete implementation
under `plugins/<category>/`. Adding a new one of an existing category (a second jurisdiction, a
second drafting language, a new LLM provider, a new notification channel, a new discovery source,
a new `Sender` backend) should never require touching `core/interfaces.py` — if your change does,
that's a signal the existing abstraction needs to change first, which is a bigger conversation
than a single new plugin.

Look at the existing plugins under `src/rtbf_agent/plugins/` for the pattern each category follows
before writing a new one; each subpackage's module docstring explains its design decisions and the
restrictions it exists to satisfy.

## Community translations of documentation

Per the spec (§6, i18n is an architecture requirement, not an add-on): project documentation
(`README.md`, `CONTRIBUTING.md`, guides) is written in English as the base language, with
`docs/i18n/` structured to receive community translations. See
[`docs/i18n/README.md`](docs/i18n/README.md) for how to contribute a translation.

This is a separate concern from the *runtime* i18n system (`plugins/i18n/`, which resolves
user-facing bot/kanban copy per interface language) — that system already exists and has its own
tests; `docs/i18n/` is specifically for translating project documentation for contributors and
users who prefer to read it in another language.

## Reporting a security issue

This project is early-stage (Phase 2) and does not yet have a formal security disclosure process.
If you find a security-relevant issue (e.g. a way to bypass the human-approval gate, a credential
leak, a way sensitive data could reach disk in plaintext), open an issue describing it — this repo
has no production users yet, so there's no urgency around public disclosure, but please still flag
it clearly as security-relevant in the title.
