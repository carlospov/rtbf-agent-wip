# User Guide: setting up and running RTBF-Agent for real

This is the guide for actually *using* this project — from a fresh clone to a real case detected,
drafted, approved, and sent to the company holding your data, over your own email account, with
follow-up handled through to a confirmed deletion or an escalation. If you're contributing code
instead, see [`CONTRIBUTING.md`](../../CONTRIBUTING.md); this page assumes you just want to run it.

Every command and config value below is checked against the actual code as of this writing, not
copied from memory — if something here stops matching reality, that's a bug in this doc, please
report it.

## What you'll need

Three pieces are required for a real end-to-end run, each covered in its own page (skip any you
don't need yet); three more are optional add-ons:

1. **[An LLM provider](llm-provider.md)** — powers classification and drafting. Anthropic, OpenAI,
   OpenRouter, opencode, or any local OpenAI-compatible endpoint (Ollama, LM Studio, vLLM). See
   [Customizing the prompts](llm-provider.md#customizing-the-prompts) for exactly which file to
   edit to change how the agent classifies or drafts.
2. **[Telegram bot](telegram-bot.md)** — one-way notifications when a draft is ready or a reply
   arrives; you still record approvals and answers separately, via the CLI or kanban UI. Entirely
   optional: skip it and you'll still see the same information printed by `process`/`serve`.
3. **[SMTP (outbound email)](smtp.md)** — how an approved request actually gets sent, to the real
   company — see [Recipient discovery](#recipient-discovery-who-a-request-actually-goes-to) below
   for how that address is determined.

Optional, all entirely skippable with no loss of core functionality:

4. **[Gmail discovery (Google Cloud OAuth)](gmail-discovery.md)** — scans your inbox to *find*
   accounts automatically, so you don't have to add every site by hand. Read-only access only, by
   construction. This is genuinely an add-on (issue #86): adding cases yourself with the
   [interactive shell](#interactive-shell)'s `add`, then advancing them with `process`, needs no
   Gmail configuration at all — Gmail discovery is one optional way *into* the pipeline, never a
   requirement for the rest of it to work.
5. **[Automatic jurisdiction/recipient suggestions](web-search.md)** (SearXNG) — a *suggestion* for
   a case's jurisdiction and real deletion-request recipient, researched via web search. A
   confident jurisdiction suggestion is applied automatically (review/override it any time before
   classification); the recipient suggestion you still always confirm/type yourself.
6. **[Sensitive document uploads](document-uploads.md)** — only needed if a company's reply asks
   you to prove your identity with a document.

## Quick start

```bash
uv sync --all-extras --dev
mkdir -p data   # .env.example's DATABASE_URL points here; it is not created automatically
uv run python -m rtbf_agent    # or: uv run forget
```

That opens the **interactive shell** (see [below](#interactive-shell)) — the *only* way this
project is driven from a terminal (alongside the [kanban web UI](#kanban-web-ui)). It does *not*
scan your Gmail inbox; that's an explicit shell command (`add`/`scan-inbox`, see below) rather than
something that happens automatically the moment you launch the tool. From there, run `setup` for a
[guided wizard](#setup-wizard) that walks through every piece above and
starts whatever real services they need — the fastest path to a working instance. Prefer editing
`.env` by hand instead? `cp .env.example .env` and fill it in using the setup pages above; every
value there is real and read by the code, none of it is a placeholder that silently does nothing
(if you find one that is, that's a bug — file an issue).

If you'd rather not set up Telegram yet, leave `RTBF_NOTIFIER_BACKEND=console` (the default) —
notifications print to your terminal instead of arriving via Telegram. Either way you always
record approvals and answers the same way: the interactive shell's decision commands
(`approve-draft`, etc., below) or the kanban UI. Good for a first dry run before wiring up the bot;
see [Telegram bot](telegram-bot.md) once you're ready for real notifications.

## Setup wizard

`setup` (a shell command) walks through five independently opt-in pieces — LLM provider, Telegram
bot, SMTP, SearXNG (web search), kanban web UI — collecting each piece's values, **validating them
with a real, non-destructive check before writing anything**, and, for the two pieces that need an
actual running service, starting that service too:

```
rtbf-agent> setup
rtbf-agent setup wizard. Answer 'y' to configure a piece, anything else to skip it.
Configure LLM provider? [y/N]: y
Known providers: anthropic, openai, openrouter, opencode (or type any other name for a
self-hosted/custom OpenAI-compatible endpoint, e.g. 'ollama').
LLM provider: anthropic
Known models for 'anthropic': claude-opus-4-5, claude-sonnet-4-5, claude-haiku-4-5
Model (blank for the provider's first known model): claude-sonnet-4-5
API key for ANTHROPIC_API_KEY (blank if none needed): sk-ant-...
LLM provider responded successfully.
Wrote RTBF_LLM_PROVIDER to /path/to/rtbf-agent/.env.
...
Configure SearXNG (web search)? [y/N]: y
About to run:
  docker run -d --name rtbf-agent-searxng -p 127.0.0.1:8080:8080 -v ... searxng/searxng:latest
Proceed? [y/N]: y
SearXNG is up at http://localhost:8080.
```

What each piece actually validates (and never sends/does for real beyond that check):

- **LLM provider** — one real, cheap completion call. Catches exactly the class of failure a bad
  model name causes (a real report: `RuntimeError: API returned 404 ... model 'mock-classify' does
  not exist`) at setup time instead of deep inside a later `process` run.
- **Telegram bot** — a real `getMe` call, plus (after auto-discovering your chat id from a message
  you send the bot, the same way [the manual steps](telegram-bot.md) already describe) a real
  confirmation message to that chat. Telegram is notify-only, so no polling loop or persistent bot
  process is needed at all — a notification is a single outbound `sendMessage` call.
- **SMTP** — a real connect and, if credentials are given, a real login. **Never sends an email.**
- **SearXNG** — actually starts a real instance via Docker (the exact config this project's own CI
  already verifies works), and only writes `RTBF_SEARXNG_BASE_URL` once it answers a real health
  check — never a URL that doesn't work yet.
- **Kanban web UI** — launches `uv run uvicorn rtbf_agent.api:app` as a background process and
  waits for `/health` before reporting it's up.

Before running a `docker`/`uvicorn` command, the wizard always shows the exact command and asks
first — starting something on your machine is treated with the same "ask before doing it" care as
any other real action here. Writing to `.env` never silently overwrites an existing value either:
if a var you're about to set already has one, you're shown both and asked.

Run `stop` (shell command) to stop whatever `setup` started (the SearXNG container, the kanban
server) — it only ever stops what it recorded starting, tracked in
`~/.config/rtbf-agent/services.json`, never guesses at unrelated processes/containers.

## Interactive shell

Running `uv run forget` (or bare `uv run python -m rtbf_agent`, no subcommand — `forget` is just a
second, more memorable name for the same entrypoint) drops you into a persistent shell, the same
shape as `claude`/`opencode`/similar tools: launch it once, then type short commands into it rather
than re-invoking a one-shot process for each action.

```
rtbf-agent interactive shell. Type 'help' for commands, 'exit' to leave.
rtbf-agent> add https://example.com/privacy
Created case for example.com (target email you@example.com).
rtbf-agent> process
[case 1] Draft ready for example.com; awaiting your approval to send.
rtbf-agent> confirm-recipient 1 privacy@example.com
case 1: recipient confirmed (privacy@example.com)
rtbf-agent> approve-draft 1
case 1: draft approved -- the next automated pass will send it
rtbf-agent> process
[case 1] sent to privacy@example.com.
rtbf-agent> show 1
Case [1] example.com (example.com)
  state: Awaiting response
  ...
rtbf-agent> exit
```

`add` -> `process` -> record decisions -> `process` (or `serve` to skip the manual re-runs) is the
complete default loop, entirely inside this one shell, and it needs no Gmail configuration at all.
**Every decision a human can record is a shell command** — this shell is the *only* CLI interface
onto this logic (alongside the kanban web UI); there is no separate standalone-subcommand form of
any command below to drop out to:

- `setup` — the [guided wizard](#setup-wizard) above.
- `stop` — stop any services `setup` started.
- `add <url>` — create a `Case` for a site you already know about, by URL (see
  [below](#adding-a-site-you-already-know-about)).
- `process [--verbose|-v]` — run one pass right now (classify/draft/recipient-confirm/approval/
  send/follow-up/resend) over every `Case`, exactly as far as an already-recorded decision allows —
  this never touches Gmail, so a case you `add`ed yourself can reach `Awaiting response` without
  any `RTBF_GMAIL_*` config existing. It never blocks waiting for you; a case with no decision
  recorded yet for its next step is simply left where it is. `--verbose`/`-v` prints a line for each
  meaningful action the pass takes (silent by default). See [below](#processing-cases-without-gmail)
  for exactly what one pass does.
- `serve [interval_minutes] [--verbose|-v]` — run `process` on a recurring interval, forever
  (default 20 minutes). Ctrl-C stops the loop and returns you to this same prompt, rather than
  exiting the shell. `--verbose`/`-v` (in any position, e.g. `serve -v`, `serve 5 --verbose`) prints
  a line for each meaningful action every pass takes; silent by default.
- `scan-inbox` — scan `RTBF_TARGET_EMAIL`'s Gmail inbox and run the full discover/draft/approve/
  send pass in one go (may open a browser for a one-time OAuth consent the first time). See
  [Running a real case](#running-a-real-case-what-actually-happens) below for exactly what one
  invocation does.
- `list` (alias `cases`) — every `Case`, grouped by kanban state.
- `show <case_id>` — one `Case`'s full details and `Event` history.
- `confirm-recipient <case_id> <email>` — set the deletion-request recipient for a `Draft ready`
  case.
- `set-jurisdiction <case_id> <value>` / `set-drafting-language <case_id> <value>` — review or
  override a case's applicable jurisdiction / drafting language while it's still `Discovered` (see
  step 2 [above](#running-a-real-case-what-actually-happens)) — refused once the case has been
  classified.
- `approve-draft <case_id>` / `reject-draft <case_id>` — approve (the next pass sends it) or reject
  (final) a case's draft — `Draft ready`, or `Requires human action` with a preview draft composed
  via `draft-additional-info` below (approving flips it to `Draft ready` right there).
- `confirm-followup <case_id>` / `dispute-followup <case_id>` — confirm a classified reply's
  outcome (closes the case) or dispute it (moves to `Requires human action` instead).
- `provide-text <case_id> <text>` / `attach-document <case_id> <path>` — record a text or document
  answer for a case in `Requires human action` (see
  [below](#answering-a-sites-request-for-more-information) for the full round trip).
- `draft-additional-info <case_id>` — compose a preview reply from the current round's recorded
  text/document answer; the case stays `Requires human action` and nothing is sent until you
  `approve-draft` it (see
  [below](#answering-a-sites-request-for-more-information) for the full round trip).
- `kanban` — prints how to start the [web kanban UI](#kanban-web-ui) (it isn't launched from
  inside the shell itself — use `setup` if you want it started for you).
- `help`, `exit` (or `quit`, or Ctrl-D).

Still not launched from inside this shell: a managed background kanban server process (`setup`
starts one for you as a separate process).

## Processing cases without Gmail

`process` (issue #86) is the shell command described above, from inside the [interactive
shell](#interactive-shell):

```
rtbf-agent> process
```

Runs classify -> draft -> recipient-confirm -> approval -> send for every `Case` already at or
before `Draft ready`, regardless of how it got there (`add`, or a prior Gmail scan) —
**no `RTBF_GMAIL_*` configuration is read or required.** Safe to re-run repeatedly, exactly like
`scan-inbox` (below): a case with nothing left to do is left untouched. There is no standalone,
non-interactive way to run this outside the shell — see the [Known current
limitations](#known-current-limitations) note on scripting/cron if that's what you were relying on
`process`/`scan-inbox` for before.

Add `--verbose`/`-v` (`process --verbose`; `serve` takes the same flag) to print a line for each
meaningful action the pass takes — a case classified or drafted, a jurisdiction/recipient proposal
recorded, a case sent, a reply found and classified, and whether the follow-up pass ran at all this
invocation (and why not if it didn't). Leave it off (the default) for exactly the same silent
output as today.

## Running a real case: what actually happens

`scan-inbox` is the whole-inbox pipeline — the pre-existing behavior this project always had before
the interactive shell became the default entrypoint, and now genuinely optional (issue #86): it
bundles Gmail discovery *and* the exact same processing `process` (above) does into one pass, for
anyone who wants sites found automatically instead of adding them by hand. Run it from inside the
shell:

```
rtbf-agent> scan-inbox
```

One invocation of `scan-inbox` does, in order:

1. **Discovery** — scans `RTBF_TARGET_EMAIL`'s Gmail inbox for account-signup/confirmation/
   password-recovery emails (first run opens a browser for a one-time OAuth consent). Each
   distinct site found becomes a `Case`, deduplicated by `(domain, target_email)` so re-running
   never creates duplicates for a site already tracked. Already know a site holds your data and
   don't want to wait for the scan to find it? See
   [Adding a site you already know about](#adding-a-site-you-already-know-about).
2. **Per-case jurisdiction/language** — for each newly-created `Case`, nothing prompts you: if
   [automatic detection](web-search.md) is configured and confidently proposes a jurisdiction, it's
   applied to the case automatically (recorded on the case's own audit trail, visible via
   `show <case_id>` or the kanban case-detail page); otherwise the field is simply left
   unset, silently deferring to the global default
   (`RTBF_DEFAULT_APPLICABLE_JURISDICTION`/`RTBF_DEFAULT_DRAFTING_LANGUAGE`) once the case is
   classified. Drafting language has no detector at all and always defers to the global default
   unless you set it yourself. Either field is still yours to review or override — but only while
   the case is still `Discovered`, before it's classified — with the interactive shell's
   `set-jurisdiction <case_id> <value>` / `set-drafting-language <case_id> <value>` commands (or the
   kanban case-detail page's two small forms). Once a case has been classified, these are locked in
   for the round and no longer editable.
3. **Classify + draft** — a real LLM call classifies the case, then a real GDPR/English legal
   template (currently the only jurisdiction/language pair implemented) drafts the actual
   deletion-request text, citing the real legal basis verbatim (never LLM-paraphrased).
4. **Recipient confirmation** — once a case reaches `Draft ready`, record the company's real
   deletion-request/privacy contact address with the shell's `confirm-recipient <case_id> <email>`
   (optionally alongside a researched suggestion shown by `show <case_id>`, see
   [Recipient discovery](#recipient-discovery-who-a-request-actually-goes-to) below) — this is a
   required, separate field from your own scanned mailbox, and you always type it yourself; nothing
   ever auto-accepts a suggestion.
5. **Approval** — you're notified a draft is ready (Telegram or console, notify-only either way),
   then record your decision whenever you get to it, asynchronously — never a live prompt waiting
   on you: the shell's `approve-draft <case_id>` or `reject-draft <case_id>`, or
   the [kanban web UI](#kanban-web-ui)'s approve/reject buttons, whichever you use first. Nothing is
   ever sent without an explicit, separately-recorded approval through one of these (see
   [Non-negotiables](../../CONTRIBUTING.md#non-negotiables)).
6. **Send** — the next `process`/`serve` pass after approval sends a real email over your
   configured SMTP account, to the recipient address confirmed in step 4 — never to your own
   scanned mailbox. A successful send moves the case straight to `Awaiting response`; there is no
   separate `Sent` state to wait in.
7. **Follow-up monitoring** — every already-sent case is checked each run for a reply. See
   [Monitoring for a reply and follow-up](#monitoring-for-a-reply-and-follow-up) below for what
   happens next (a confirmed deletion, an escalation, or a request for more information).

Safe to re-run repeatedly: a `Case` already resolved one way or another is left untouched — no
re-notification, no re-send, no re-prompt; a case with nothing new to report (no reply yet) is
likewise left completely untouched, with no timeout or auto-escalation ever applied to it.

### Reviewing a draft before you approve it

The Telegram/console notification itself only shows a case summary, not the full drafted text.
To actually read what you're about to approve, either:

- Visit the [kanban web UI](#kanban-web-ui) (`http://127.0.0.1:8000/cases/<id>` once a case
  exists) — this is the easiest way, and shows the complete drafted subject/body.
- Or run the shell's `show <case_id>` — the CLI's equivalent of the kanban detail page, showing
  the same full `Event` history (including the drafted subject/body) in the terminal.
- Or query the database directly. With the `sqlite3` CLI (if installed):
  ```bash
  sqlite3 data/rtbf-agent.db "SELECT detail FROM event WHERE kind='draft_proposed' ORDER BY id DESC LIMIT 1;"
  ```
  Without it, Python's stdlib `sqlite3` module works identically and needs nothing extra installed:
  ```bash
  python3 -c "
  import sqlite3
  conn = sqlite3.connect('data/rtbf-agent.db')
  print(conn.execute(\"SELECT detail FROM event WHERE kind='draft_proposed' ORDER BY id DESC LIMIT 1\").fetchone())
  "
  ```

## Recipient discovery: who a request actually goes to

A `Case` tracks two, entirely separate email addresses — mixing them up would mean either emailing
yourself instead of the company, or never having a real recipient at all:

- `target_email` — **your own** scanned mailbox (the account discovery found this case in).
- `recipient_email` — the **company's** real deletion-request/privacy contact. This is what a send
  actually goes to, and it's required before any approval is even solicited: a `Case` with no
  recipient confirmed yet is never offered for approval, and the send path itself refuses to run
  without one, as a second, unbypassable check.

You always type this address yourself, at the point described in step 4 above. If
[automatic detection](web-search.md) is configured, a researched suggestion is shown alongside the
prompt, explicitly labeled unverified — but a blank answer never accepts it; you must type the
address, confirming you've actually looked at it.

## Adding a site you already know about

You don't have to wait for the inbox scan to surface a site. If you already know a company holds
your data, add a `Case` for it directly by URL, from inside the [interactive shell](#interactive-shell):

```
rtbf-agent> add https://example.com
```

This creates a `Case` for the pasted site's domain (a full URL or a bare `example.com` both work),
attributed to your `RTBF_TARGET_EMAIL`, using the exact same `(domain, target_email)` dedup key the
inbox scan uses — so when both paths land on the same domain for a site, they converge on one
`Case` instead of creating a duplicate. Just like step 2 above, nothing prompts you here either: if
[automatic detection](web-search.md) is configured and finds a confident jurisdiction suggestion,
it's applied to the case automatically, with zero interaction — the same no-prompt behavior a
Gmail-discovered case gets, not something deferred to a later run. Review or override it (or set a
drafting language) any time before the case is classified with `set-jurisdiction`/
`set-drafting-language` (see step 2 above). The next `process` (or `scan-inbox`) run then picks the
new `Case` up from `Discovered` and drafts/sends it exactly as it would any other. `add` requires a
configured LLM provider (see [Setup wizard](#setup-wizard) or
[What you'll need](#what-youll-need)) — needed to research the jurisdiction suggestion.

The [kanban web UI](#kanban-web-ui) has the same capability — a small form at the top of the board
page (`POST /cases`), same underlying `add_case_for_url()` call, same dedup/jurisdiction-auto-apply
behavior, no Gmail dependency either. Use whichever surface you already have open; both write to
the same database.

## Monitoring for a reply and follow-up

Every `scan-inbox` run also checks each already-sent case for a new reply, and classifies it if one
arrived — never on any deadline, and never as a substitute for you reading it yourself:

- **No reply yet** — the case is left completely untouched. There is no timeout, no auto-close, and
  no auto-escalation on a case waiting for a response, however long that takes.
- **The company confirms the deletion, or refuses/escalates** — this is only ever a *proposal*
  until you explicitly confirm it: you're asked to approve closing the case as confirmed or
  rejected, quoting the company's actual reply text (not just the agent's summary of it) so you can
  judge for yourself. If you disagree with the agent's reading, the case moves to
  `Requires human action` for you to resolve manually, instead of being silently closed wrong.
- **The company asks for more information or an identity document** — the case moves to
  `Requires human action` and you're notified what's being asked for. See
  [Answering a site's request for more information](#answering-a-sites-request-for-more-information)
  below.

If a case sits in `Awaiting response` with no progress and you're using the Gmail backend (the
default) for reply monitoring, check your logs for a warning about a missing cached OAuth token —
the fix is running `scan-inbox` once to establish it. `process`/`serve` log a warning whenever this
is the reason a pass isn't checking for replies at all.

### Reply monitoring without Gmail (IMAP)

Everything above works identically whether replies are read via Gmail (the default) or via plain
IMAP — set `RTBF_REPLY_MONITOR_BACKEND=imap` if you don't have (or don't want) a Gmail OAuth app
configured, or your mailbox is on a different provider entirely. Configure your own mailbox's IMAP
credentials:

```bash
RTBF_REPLY_MONITOR_BACKEND=imap
RTBF_IMAP_HOST=imap.example.com
RTBF_IMAP_PORT=993          # optional -- 993 (IMAP-over-TLS) is the default
RTBF_IMAP_USERNAME=you@example.com
RTBF_IMAP_PASSWORD=your-mailbox-password-or-app-password
```

(All four can also live under `[imap]` in `~/.config/rtbf-agent/config.toml` instead of the
environment, exactly like `RTBF_SMTP_*`/`[smtp]` above.) The connection is always TLS
(`imaplib.IMAP4_SSL`) — there is no plaintext-IMAP option.

Unlike Gmail's OAuth handshake, an IMAP username/password login needs no interactive step at all —
once `RTBF_IMAP_*` is configured, `process`/`serve` pick up replies from the very first unattended
pass, with nothing equivalent to `scan-inbox`'s one-time interactive setup required. Gmail
*discovery* (finding new accounts by scanning your inbox) is unaffected either way and stays
Gmail-only — `RTBF_REPLY_MONITOR_BACKEND` only changes how *replies* to already-sent requests are
read; if you want automatic discovery too, use `scan-inbox` (above) instead, which needs Gmail
configured, or add sites yourself with `add`, which needs no Gmail configuration at all.

## Answering a site's request for more information

If a case is sitting in `Requires human action` because a company's reply asked for more
information or an identity document, answer it from inside the [interactive
shell](#interactive-shell):

```
rtbf-agent> provide-text <case_id> "here is the information they asked for"
# or, for a document:
rtbf-agent> attach-document <case_id> /path/to/id-scan.png
```

These only *record* your answer — the case stays `Requires human action`, and nothing is drafted or
sent yet, however many notes/documents you add. When you're ready, compose a preview:

```
rtbf-agent> draft-additional-info <case_id>
```

This drafts an LLM-composed reply from your note (or a plain "please find the attached document(s)"
if you only provided a document) — still only a *preview*: the case stays `Requires human action`,
nothing has been sent, and you can review it (`show <case_id>`, or the case's kanban detail
page — which now shows the same review/edit/approve/reject controls a first-time draft gets, right
on the same page, without the case appearing to change kanban column) or hand-edit it
(`edit-draft`) before committing to anything. Only actually approving it authorizes a send, through
the *same* approval gate as any other send:

```
rtbf-agent> approve-draft <case_id>
```

— nothing goes out without this (kanban or the shell) — the original send's approval is never reused for
this one. A document you provide goes through the same encrypted-storage/secure-delete lifecycle as
any other sensitive document — see [Sensitive document uploads](document-uploads.md) for the full
details (encryption key setup, TTL, what "purged" means). Once sent, the case returns to `Awaiting
response` and the normal follow-up monitoring above picks it up again — this can repeat as many
rounds as a company's back-and-forth actually requires.

## Kanban web UI

A view of every case and its full audit trail, plus (for a case in `Draft ready`) a second,
equally-valid approve/reject surface alongside Telegram/console — run alongside the CLI (they
share the same database, so nothing needs syncing):

```bash
uv run uvicorn rtbf_agent.api:app --reload
```

Then visit `http://127.0.0.1:8000/` — the kanban board, every case grouped by state. Click a card
to see its full event history, including the complete drafted request text. A case in `Draft ready`
shows **Approve**/**Reject** buttons right on its detail page:

- **Approve here** only *records* your decision — this process has no email-sending
  configuration wired into it. The next `process` (or `scan-inbox`) run picks up the recorded
  decision and sends.
- **Reject here** takes full effect immediately: the case moves to `Rejected/Escalated` right away,
  the same as the shell's `reject-draft`.
- Whichever channel — the interactive shell's decision commands or kanban — you use first is the
  one that counts; the send still only ever happens once, regardless of which channel records the
  decision first.

Both the Telegram bot's and the kanban board's own interface text (button labels, page copy — not
a drafted request's content) follow `INTERFACE_LANGUAGE` (defaults to `en`), independently of any
per-case `drafting_language`/`applicable_jurisdiction` set above.

## Safety checklist for your first real run

Read this before you set anything up for real:

1. **A real approved send reaches the actual company** (the address you confirmed as
   `recipient_email` — see [Recipient discovery](#recipient-discovery-who-a-request-actually-goes-to)
   above), not just your own mailbox. Approving a send is a real, external, irreversible action —
   don't approve one you're not prepared to actually have delivered. If you want to safely watch
   the whole pipeline work end to end without a real send reaching anyone, either don't press
   approve, or point `RTBF_SMTP_*` at a mail-trap/test SMTP account instead of a real one for your
   first dry run.
2. **Use a secondary/test Gmail account for `RTBF_TARGET_EMAIL`**, not your primary inbox, the
   first time — least-privilege for code you're trusting for the first time, independent of point
   1 above.
3. **Verify the Google Cloud OAuth consent screen only requests the read-only Gmail scope**
   (`gmail.readonly`) before completing the browser login — don't just trust the code comment,
   check the consent screen yourself. See [Gmail discovery](gmail-discovery.md).
4. **Use an app password for SMTP**, never your real account password. See [SMTP](smtp.md).
5. **Use `.env.example`'s shipped `DATABASE_URL`** (`./data/rtbf-agent.db`, already gitignored —
   note this differs from the plain code-level fallback, `./rtbf-agent.db` in the repo root, used
   only if `DATABASE_URL` is completely unset) so you can freely delete and restart between
   attempts: `rm data/rtbf-agent.db`.
6. Run `uv run pytest` once locally first, to confirm you're on a tested `main` state before
   trusting a real run against your own accounts.

## Troubleshooting

- **`sqlite3.OperationalError: unable to open database file`** — the directory in your
  `DATABASE_URL` doesn't exist yet (nothing creates it automatically). Fix: `mkdir -p data` (or
  whatever directory your `DATABASE_URL` points at).
- **`LookupError: no LLM provider selected`** — `RTBF_LLM_PROVIDER` is unset. See
  [LLM provider](llm-provider.md).
- **`LookupError: no API key resolved for LLM provider '...'`** — you set `RTBF_LLM_PROVIDER` but
  not the matching API key var (e.g. `ANTHROPIC_API_KEY` for `anthropic`). See
  [LLM provider](llm-provider.md).
- **`LookupError: no Gmail OAuth client configured`** — `RTBF_GMAIL_CLIENT_SECRETS_FILE` is unset
  or points at a missing file. See [Gmail discovery](gmail-discovery.md).
- **`LookupError: incomplete SMTP config: missing ...`** — one of `RTBF_SMTP_HOST`/
  `RTBF_SMTP_FROM_ADDRESS`/(`RTBF_SMTP_PASSWORD` if a username is set) is missing. See
  [SMTP](smtp.md).
- **`LookupError: incomplete Telegram config: missing ...`** — `RTBF_NOTIFIER_BACKEND=telegram`
  but `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` aren't both set. See [Telegram bot](telegram-bot.md).
- **`ValueError: RTBF_DOCUMENT_ENCRYPTION_KEY is not set`** — only relevant if you're providing a
  document via the shell's `attach-document`. See [Sensitive document uploads](document-uploads.md).
- **A jurisdiction/recipient suggestion never appears, and you expected one** — automatic detection
  is optional and silently off if unconfigured, or if the detector itself fails. See
  [Automatic jurisdiction/recipient suggestions](web-search.md#troubleshooting).
- **OAuth token refresh fails on a later run** (revoked/expired token) — the CLI automatically
  falls back to a fresh browser login; you don't need to manually delete the cached token file
  (`~/.config/rtbf-agent/gmail_token.json` by default).
- **A case in `Awaiting response` never seems to be checked for a reply, and you're on the Gmail
  backend** — see [Monitoring for a reply and follow-up](#monitoring-for-a-reply-and-follow-up)
  above: run `scan-inbox` once to establish a cached OAuth token, or switch to
  [the IMAP backend](#reply-monitoring-without-gmail-imap).
- **A drafted request has a blank signature line** — `RTBF_REQUESTER_NAME` is unset. This is
  cosmetic, not an error (you'll notice it before approving the send).

## Known current limitations

- **No standalone, non-interactive subcommand for scripting/cron**: every action (`process`,
  `scan-inbox`, approving a draft, ...) is now only reachable from inside the interactive shell —
  a deliberate maintainer decision to have exactly one CLI surface, made once every former
  standalone subcommand had a shell equivalent. If you were driving `process`/`scan-inbox` from
  cron before, that framing no longer applies; `serve [interval_minutes]` (from inside the shell)
  is the built-in way to get a recurring, unattended loop instead.
- **Jurisdiction/language coverage**: only GDPR + English exist right now. A `Case` for a site in
  a jurisdiction without a template raises a clear error naming the missing jurisdiction/language
  pair, rather than drafting something wrong.
- **A reply from a third-party ticketing domain can go unnoticed**: follow-up monitoring matches a
  reply by the recipient's own domain; a company that replies from a different domain entirely
  (e.g. a helpdesk platform like `company.zendesk.com` rather than `company.com`) won't be picked
  up automatically. Safe either way (nothing auto-escalates), but you may need to follow up on such
  a case manually.
- **The sensitive-document TTL purge isn't yet on an automatic schedule** — an unapproved document
  is still never plaintext and never sent without approval, but the 24-hour cleanup itself
  currently needs to be triggered rather than running unattended. See
  [Sensitive document uploads](document-uploads.md#known-limitations).
- **No real-time push between Telegram and the kanban board** — if you approve/reject via one
  channel, refresh the kanban page to see the case's now-updated state; it doesn't update live.
