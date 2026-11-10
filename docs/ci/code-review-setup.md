# AI code review setup (Open Code Review + opencode Zen)

`.github/workflows/code-review.yml` runs [Open Code Review](https://github.com/alibaba/open-code-review)
(`ocr`) on every PR via its official composite GitHub Action, posting inline + a sticky summary
comment. This is a review aid, not a gate — it doesn't block merges. It's separate from (and a
complement to, not a replacement for) the `code-reviewer` subagent invoked per `CLAUDE.md`, which
checks this repo's specific non-negotiables (human-in-the-loop, no plaintext secrets, no hardcoded
i18n logic). OCR does general-purpose defect-finding (bugs, security, style); `code-reviewer` does
repo-specific rule enforcement.

## One-time setup

You need an opencode Zen API key: sign in at [opencode.ai](https://opencode.ai/docs/zen/), add
billing details (free-tier models don't require spend, but the account still needs the billing
step), and copy your API key.

**Verified directly against the live endpoint** (not just docs-scraped) on 2026-07-12:
`GET https://opencode.ai/zen/v1/models` is public, unauthenticated, and lists the current model
IDs (plain strings, no `opencode/` prefix); a `POST` to
`https://opencode.ai/zen/v1/chat/completions` with a bogus `Authorization: Bearer` token returns a
clean `401 {"type":"AuthError","message":"Invalid API key."}`, confirming the endpoint path, model
ID format, and that it's a standard `Authorization: Bearer` header — no `OCR_LLM_AUTH_HEADER`
override needed. Free models live at the time of writing: `big-pickle`, `deepseek-v4-flash-free`,
`mimo-v2.5-free`, `hy3-free`, `nemotron-3-ultra-free`, `north-mini-code-free`. Re-run
`curl https://opencode.ai/zen/v1/models` before wiring config, since this list can change.

OCR's `openai` protocol expects a base URL (it appends `/chat/completions` itself, standard
OpenAI-SDK behavior), so `OCR_LLM_URL` is the Zen base `https://opencode.ai/zen/v1`, not the full
completions path. (If you instead pick an Anthropic-shaped Claude model on Zen, switch
`OCR_LLM_USE_ANTHROPIC` to `true` and point `OCR_LLM_URL` at `https://opencode.ai/zen/v1/messages`
instead — untested, the `chat/completions` path above is the one actually verified live.)

## Commands to run (with your real key — never share it with an agent session)

```bash
# Secrets (never printed in logs, not visible via `gh variable list`)
gh secret set OCR_LLM_URL --body "https://opencode.ai/zen/v1"
gh secret set OCR_LLM_AUTH_TOKEN --body "<your opencode Zen API key>"

# Variables (fine to be visible — not secret)
gh variable set OCR_LLM_MODEL --body "deepseek-v4-flash-free"   # or any free model listed above
gh variable set OCR_LLM_USE_ANTHROPIC --body "false"
```

## Verifying it worked

Open any PR against `main` after the secrets/variables are set — the workflow runs automatically
and posts a summary comment. To check without opening a PR:

```bash
gh workflow run "AI Code Review" --ref <some-open-pr-branch>   # will no-op outside a PR context
gh run list --workflow=code-review.yml
```

If the run fails with an auth error, double check the key was copied correctly and that the model
you picked is still listed at `https://opencode.ai/zen/v1/models` — free-tier models are offered
during limited evaluation windows and can be rotated out.

## Switching providers later

Nothing in `code-review.yml` is opencode-specific beyond the doc comment — swap the four
secrets/variables to point at any other OCR-supported provider (built-in preset name via
`ocr config set provider <name>` locally to find the right `url`/`protocol`, or any other
OpenAI-/Anthropic-compatible endpoint) without touching the workflow file itself. See
[`docs/architecture/llm-provider-patterns.md`](../architecture/llm-provider-patterns.md) for why
that's possible with only two protocol branches.
