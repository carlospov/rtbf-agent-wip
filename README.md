# RTBF-Agent

> **Work in progress.** This is an unfinished, pre-alpha project, published as a snapshot of a
> private repository (so the commit history starts here). It covers only GDPR and English for now,
> and some documents in this repo describe plans that are not built yet. It was also used as a
> real project to try out [Open Code Review](https://github.com/alibaba/open-code-review), an AI
> reviewer that comments on every pull request.

A human-in-the-loop case manager for exercising your own right to be forgotten. It discovers
which sites hold accounts tied to your email, drafts legally-grounded deletion requests, tracks
responses, and requires your explicit, separately-recorded approval — via the interactive shell or
a web kanban — before any irreversible action (sending a request, attaching an ID document,
anything sensitive). A recurring loop (the shell's `serve` command) is the only thing that ever
acts on those decisions; Telegram notifies you but never itself records one.

> **Status: Phase 3, in progress.** Discovery (Gmail), a real LLM, real GDPR/English legal
> templates, a real SMTP send, an interactive management shell (`forget`, the *only* CLI entrypoint
> — see below), a recurring `serve` shell command, a Telegram bot for one-way notifications, and a
> kanban web UI (including its own approve/reject) all exist and are wired together end-to-end,
> gated on your explicit, separately-recorded approval at send-time. See
> [`docs/user-guide/`](docs/user-guide/README.md) for full
> setup instructions, [`ROADMAP.md`](ROADMAP.md) for the phased plan,
> [`tasks/todo.md`](tasks/todo.md) for the current checklist, and
> [`docs/specs/2026-07-12-rtbf-agent-requirements.md`](docs/specs/2026-07-12-rtbf-agent-requirements.md)
> for the full requirements doc.

A project by [outlaw-labs](https://github.com/carlospov).

## Principles

- **Human in the loop.** The agent never sends an email, uploads an ID, or signs anything without
  your explicit approval at the moment of action.
- **Full, immutable audit trail.** Every action — including proposals you didn't approve — is
  logged with actor, timestamp, and detail.
- **Minimal sensitive data at rest.** ID documents are encrypted, short-TTL, and securely deleted
  after use; the audit log never stores document content, only hashes.
- **No centralized backend.** Everything runs on your machine with your own credentials (your own
  Telegram bot, your own email access). Zero telemetry.
- **For your own accounts only.** Not designed or licensed for managing anyone else's data without
  their explicit consent.
- **Extensible by design.** New language, jurisdiction, email provider, or discovery source = new
  plugin, never a core-code change.

## LLM provider

Swappable by design — Claude, OpenAI, OpenRouter, opencode, or any OpenAI-compatible endpoint
(Ollama, LM Studio, vLLM...) for fully local/self-hosted use. Configured via `.env`
(`.env.example` has the full list), never hardcoded.

## Development

```bash
uv sync --all-extras --dev
cp .env.example .env   # fill in your own credentials — never commit this file
uv run pytest
```

See [`CLAUDE.md`](CLAUDE.md) for the full command list and repo conventions, or
[`CONTRIBUTING.md`](CONTRIBUTING.md) for the full contribution workflow.

## Running a real case

```bash
uv sync --all-extras --dev
cp .env.example .env   # fill in your own credentials — never commit this file
uv run forget   # or: uv run python -m rtbf_agent
```

That opens an interactive shell — the *only* way this project is driven from a terminal (in the
style of `claude`/`opencode`): launch it once, then type short commands into it. **Every action
below — including `serve`, `scan-inbox`, and every decision-recording command — is a shell
command**; there is no separate standalone-subcommand form of any of them to drop out to. The
[web kanban UI](#web-kanban-ui) is the only other way in:

```
rtbf-agent> setup   # optional guided wizard -- see docs/user-guide/README.md#setup-wizard
rtbf-agent> add https://example.com/privacy
Created case for example.com (target email you@example.com).
rtbf-agent> process
[case 1] example.com: Draft ready
rtbf-agent> confirm-recipient 1 privacy@example.com
case 1: recipient confirmed (privacy@example.com)
rtbf-agent> approve-draft 1
case 1: draft approved -- the next automated pass will send it
rtbf-agent> serve
forget serve: running every 20.0 minute(s) (Ctrl-C to stop)...
^C
serve: stopped
rtbf-agent> exit
```

(Ctrl-C stops `serve` and returns you to the shell prompt, rather than exiting the whole shell —
run it in a separate terminal instead if you want it to keep going while you do other things.)

**The shell's `serve` command is the way this project is meant to run day to day.** It's a
recurring loop, not a one-shot command: every interval (default 20 minutes, `serve
<interval_minutes>` or `RTBF_SERVE_INTERVAL_MINUTES` to change it) it advances every `Case` exactly
as far as an already-recorded human decision allows — classify, draft, and (once you've confirmed a
recipient and approved the draft) send; check `Awaiting response` cases for a reply and, once
you've confirmed or disputed a classified outcome, close them; resend once you've supplied
requested additional info — and leaves anything else untouched. Nothing in this pipeline ever
blocks waiting for you: **you only ever act asynchronously**, via the shell's decision commands
(below) or the [web kanban UI](#web-kanban-ui), whenever you get to it. Needs no Gmail configuration
at all (issue #86 — Gmail discovery is an optional add-on for finding sites automatically, never a
requirement for processing ones you already have), and reuses a cached Gmail OAuth token for
follow-up monitoring if one already exists (run `scan-inbox` once to establish it) without ever
attempting an interactive login of its own — an unattended loop has no browser to complete one in.

Pass `--verbose`/`-v` (`process -v`, `serve --verbose`, `serve 5 -v`) to print a line for each
meaningful action a pass takes — a case classified or drafted, a jurisdiction/recipient proposal
recorded, a case sent, a reply found and classified, whether the follow-up pass ran at all this
invocation (and why not if it didn't), and a resend triggered. Leave it off (the default) and both
commands stay exactly as silent as before.

If a case sits in `Awaiting response` with no progress and you're using the Gmail backend for
reply monitoring, check your logs for a warning about a missing cached OAuth token — the fix is
running `scan-inbox` once to establish it; `process`/`serve` warn in the log if that's why
follow-up monitoring isn't running. Don't want to depend on Gmail for replies at all? See [Reply
monitoring without Gmail (IMAP)](docs/user-guide/README.md#reply-monitoring-without-gmail-imap) in
the user guide.

While a case is in `Draft ready`, record your decisions from inside the shell with:

```
rtbf-agent> cases                              # list every case, grouped by state
rtbf-agent> show <case_id>                     # full detail + event history
rtbf-agent> confirm-recipient <case_id> <email>  # set the real deletion-request recipient
rtbf-agent> approve-draft <case_id>            # the next 'serve'/'process' pass will send it
rtbf-agent> reject-draft <case_id>             # final -- moves to Rejected/Escalated
```

Once a reply arrives and is classified, confirm or dispute the agent's reading:

```
rtbf-agent> confirm-followup <case_id>   # closes as Deletion confirmed / Rejected-Escalated
rtbf-agent> dispute-followup <case_id>   # moves to Requires human action instead
```

The shell's `process` command runs exactly one pass right now, without waiting for `serve`'s
interval — useful just to see something happen immediately after recording a decision (there is no
standalone, scriptable/cron-friendly form of this outside the shell — see [the user
guide](docs/user-guide/README.md#known-current-limitations)).

Full Gmail inbox discovery (the pre-existing pipeline this project always had) is the `scan-inbox`
shell command — it bundles discovery *and* the same processing `process`/`serve` does into one
pass, for anyone who wants sites found automatically instead of adding them by hand:

```
rtbf-agent> scan-inbox
```

Scans `RTBF_TARGET_EMAIL`'s Gmail inbox for account-signup/confirmation/password-recovery mail
(first run opens a browser for a one-time, read-only OAuth consent —
`RTBF_GMAIL_CLIENT_SECRETS_FILE` must point at your own OAuth client secrets JSON), creates a
`Case` for each newly-discovered account, classifies and drafts a real GDPR-cited deletion request
for each, and — once you've confirmed a recipient and approved the draft (kanban or CLI, above) —
the next pass sends it over your own SMTP account. Safe to re-run repeatedly: cases already sent,
rejected, or otherwise past the draft stage are left untouched (no re-notify, no re-send).

### Adding a site you already know about

You don't have to wait for the inbox scan to surface a site. If you already know a company holds
your data, add a `Case` for it directly by URL, with the shell's `add <url>` command shown above:

```
rtbf-agent> add https://example.com
```

This creates a `Case` for the pasted site's domain (a full URL or a bare `example.com` both work),
attributed to your `RTBF_TARGET_EMAIL`, using the exact same `(domain, target_email)` dedup key the
inbox scan uses — so when both paths land on the same domain for a site, they converge on one
`Case` instead of creating a duplicate. The next `process`/`serve` (or `scan-inbox`) run picks the
new `Case` up from `Discovered` and drafts/sends it exactly as it would any other.

### Answering a site's request for more information

If a site's reply asks for additional verification (personal info, or an identity document) before
it will process your deletion request, the case moves to `Requires human action` and you get a
notification describing what's being asked for. Answer it from inside the shell:

```
rtbf-agent> provide-text <case_id> "here is the information they asked for"
# or, for a document:
rtbf-agent> attach-document <case_id> /path/to/id-scan.png
```

These only *record* your answer — nothing is drafted or sent yet, and the case stays `Requires
human action` while you keep adding to it. When you're done for this round, compose a preview:

```
rtbf-agent> draft-additional-info <case_id>
```

This composes an LLM-drafted reply from your note (or a plain "please find the attached document(s)"
if you only provided a document) — still *only a preview*: the case stays `Requires human action`,
and nothing has been sent. Review it (`show <case_id>`, or the case's kanban detail
page, which now shows the same edit/approve/reject controls a first-time draft gets, right there,
without the case appearing to move columns), edit it if you want (`edit-draft`), then send it
through the *same* approval gate as any other send:

```
rtbf-agent> approve-draft <case_id>
```

Only this step actually authorizes a send — the next `process`/`serve` pass dispatches it. A
document you provide goes through the same encrypted-storage/secure-delete lifecycle as any other
sensitive document (never written to disk unencrypted). Once sent, the case returns to `Awaiting
response` and the normal follow-up monitoring picks it up again — this can repeat as many rounds as
a site's back-and-forth actually requires.

**Full setup walkthrough** (LLM provider, Gmail OAuth, Telegram bot, SMTP, safety checklist,
troubleshooting): [`docs/user-guide/`](docs/user-guide/README.md).

## Web kanban UI

A FastAPI app backs the web kanban (issue #53 for the app skeleton; issue #54 adds the kanban board
below; issue #55 adds the per-case detail/event audit trail view each card links to; issue #67
adds its approve/reject actions, see below).
Frontend approach: server-rendered Jinja2 templates, not a separate SPA — see the module docstring
in [`src/rtbf_agent/api/app.py`](src/rtbf_agent/api/app.py) for the full rationale. It reads/writes
the exact same `DATABASE_URL` database the CLI does (`src/rtbf_agent/db.py`) — no second store, so
the kanban board always matches what the CLI/Telegram bot last wrote, no drift.

Run it locally (or run `setup` from the interactive shell to have it started for you, issue #89):

```bash
uv sync --all-extras --dev
cp .env.example .env   # DATABASE_URL, same as the CLI — see .env.example
uv run uvicorn rtbf_agent.api:app --reload
```

Then visit `http://127.0.0.1:8000/` (redirects to `/cases`, the kanban board: every `Case` grouped
into columns by kanban state), `http://127.0.0.1:8000/cases/{id}` (a single `Case`'s full `Event`
history in chronological order — including a drafted request's full body/subject, so you can
review its actual content here before approving it), `http://127.0.0.1:8000/health` (liveness
check), or `http://127.0.0.1:8000/cases/count` (total `Case` row count). The interface language is
`INTERFACE_LANGUAGE` — the same env var, and same resolution order, the Telegram bot uses (see
`resolve_interface_language()` in `src/rtbf_agent/config.py`); it's independent of any per-case
`drafting_language`/`applicable_jurisdiction`.

A `Case` in "Draft ready" can be approved or rejected right from its detail page — an equally-valid
channel alongside the shell's `approve-draft`/`reject-draft` commands, not a replacement for them;
Telegram itself is notify-only (see below), never a decision-recording channel of its own. Approving
here only records the decision (this process has no SMTP wiring); the next `process`/`serve` run
picks it up and sends. Whichever channel — kanban or the shell — records the decision first wins;
the send still only ever happens once, through the same approval-gated path either way.

## Out of scope

- The agent never resolves identity verification on its own (no filling identity forms, no
  signing, no generating/editing ID images).
- Not for managing deletion of third parties' data without their consent.
- No hosted/centralized service — self-run only.
- Deletion on third-party infrastructure (Telegram, email providers) is only as reliable as those
  platforms allow; this project makes no guarantee beyond that.
- Once a document you upload transits Telegram's own infrastructure (its Bot API), what happens to
  it there is outside this project's control — that's a platform limitation, not something this
  project's encrypted-storage-and-secure-delete lifecycle (see below) can guarantee against.

## Sensitive document handling

Identity documents (e.g. a passport scan sent to prove identity to a company) are never written to
disk in plaintext: they're encrypted at rest (`RTBF_DOCUMENT_ENCRYPTION_KEY`), automatically purged
via secure delete (overwrite, not a plain unlink) if not approved for sending within a TTL (default
24h), and secure-deleted immediately after being attached and sent. See
`src/rtbf_agent/document_store.py`.

## License

[Apache 2.0](LICENSE)
