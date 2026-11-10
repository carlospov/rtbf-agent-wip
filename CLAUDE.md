# CLAUDE.md — RTBF-Agent

Human-in-the-loop right-to-be-forgotten case manager. Discovers accounts tied to a user's email,
drafts and tracks GDPR/CCPA/etc. deletion requests, and gates every irreversible action (sending a
request, attaching an ID document) on explicit human approval via Telegram or a web kanban.

This repo is also the public showcase for `outlaw-labs` — hold code and docs to that bar from the
first commit, not just once it "works."

Full requirements: `docs/specs/2026-07-12-rtbf-agent-requirements.md`. Current planning layer:
`docs/planning/OBJECTIVES.md` (high-level objectives, open tasks) and
`docs/planning/RESTRICTIONS.md` (numbered, traceable constraints — `R1`...`R30`). `tasks/todo.md`
holds the concrete step-by-step plan once restrictions are translated into functionality + tests;
until then its current content is marked superseded.

## Stack

Python 3.11+, `uv` for deps/venv, `ruff` for lint+format, `ty` for type checking, `pytest` for
tests, FastAPI + SQLModel (SQLite) for the core/API, `python-telegram-bot` for the bot (not
installed yet — added in Phase 2, see `tasks/todo.md`).

## Commands

```bash
uv sync --all-extras --dev   # install deps
uv run pytest                # test — run before claiming anything works
uv run ruff check .          # lint
uv run ruff format .         # format
uv run ty check src          # type check
```

CI (`.github/workflows/ci.yml`) runs all four plus `uv build` on every push/PR to `main`.

## Non-negotiables (see spec section 0/2 for full rationale)

- **No irreversible action without human approval at send-time.** Never auto-send a request,
  auto-attach a document, or auto-sign anything.
- **No hardcoded language/jurisdiction/site logic in `core/`.** That belongs in `plugins/`, behind
  an interface in `core/interfaces.py`.
- **LLM calls go through `LLMProvider`, never a concrete SDK directly.** Must work identically
  against Anthropic, OpenAI, OpenRouter, or a local OpenAI-compatible endpoint (Ollama, vLLM...).
- **No sensitive data in plaintext at rest.** Identity docs are always encrypted at rest
  (`document_store.py`, Fernet). Retention is the **document vault** policy (issue #111, a
  deliberate maintainer decision superseding this project's earlier "short TTL, secure delete
  after use" default): a vault document has **no automatic expiry** and is kept, encrypted,
  until the human explicitly deletes it (`delete_vault_document()`) — reuse across cases/rounds is
  the point. Secure-delete (overwrite + unlink, not a bare `unlink`) still applies whenever a
  document *does* get removed, whether by explicit deletion or the legacy TTL sweep that still
  covers documents uploaded before #111. Audit log (`Event`): hash + metadata only, never content.
- **Every new module in `core/` or `plugins/` ships with tests.** Not "added later."

## Workflow

- Non-trivial change → plan mode first (`Shift+Tab`). Update `tasks/todo.md`, don't just talk
  through it in chat.
- One commit per completed checklist item in `tasks/todo.md` (easy revert if something's wrong).
- Before claiming a task is done: actually run `uv run pytest` and `uv run ruff check .` — don't
  assert it passes without running it.
- Use the `code-reviewer` subagent (`.claude/agents/code-reviewer.md`) before merging any PR that
  touches `src/` — it checks the non-negotiables above specifically.
- CI also runs [Open Code Review](https://github.com/alibaba/open-code-review) on every PR
  (`.github/workflows/code-review.yml`, setup: `docs/ci/code-review-setup.md`) for general-purpose
  defect-finding (bugs, security, style) as a non-blocking comment bot — it's a complement to
  `code-reviewer`, not a replacement; it doesn't know this repo's specific non-negotiables.
- Update docs (README, spec deltas) in the same PR as the code change they describe, not after.

## Gotchas

- **Never copy Spanish terminology from the spec into code identifiers.**
  `docs/specs/2026-07-12-rtbf-agent-requirements.md` is written in Spanish and stays that way (it's
  the original business/legal spec, not code) — but every field name, enum value, function name,
  and variable name in the actual code must be in English. Translate the *concept* the spec
  describes, never transliterate the *term* it uses. This already happened once: `core/models.py`
  was originally implemented by copying spec §3.2's `Case` field list (`sitio`, `dominio`,
  `estado`, `jurisdicción_aplicable`, `idioma_comunicacion`, ...) and its kanban state names
  verbatim as Python identifiers — inconsistent with the rest of the (English-only) codebase, and
  it had to be renamed after the fact once a human caught it. Before naming anything derived from
  a spec section, translate it first.
