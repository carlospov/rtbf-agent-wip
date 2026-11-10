# Telegram bot setup

Telegram is **notify-only**: it sends you a one-way message when a draft is ready, a reply arrived,
or more information is needed, but it never solicits or receives a decision itself — no inline
buttons, no blocking wait for a reply. You record every decision asynchronously, whenever you get
to it, through the interactive shell's decision commands (`approve-draft`, `reject-draft`,
`confirm-followup`, `dispute-followup`, `provide-text`, `attach-document`) or the web kanban UI.
Implemented in `src/rtbf_agent/plugins/notifiers/telegram.py`, wired into the CLI via
`RTBF_NOTIFIER_BACKEND`.

This is optional for a first dry run — leave `RTBF_NOTIFIER_BACKEND=console` (the default) to skip
Telegram notifications entirely and come back to this page later; you record decisions the same
way either way.

## Why a whitelisted `chat_id`, and why you must get it *before* running the bot for real

This project is "one bot per user, self-hosted" — your bot only ever talks to one Telegram chat
(yours), enforced from the very first message it ever receives, with no bootstrap window where an
unwhitelisted chat is allowed through. That means you can't just start the bot and message it to
"register" yourself — the bot will silently ignore any message from a chat that isn't already the
configured `TELEGRAM_CHAT_ID`. You need to know your own `chat_id` *before* setting
`RTBF_NOTIFIER_BACKEND=telegram` and running for real.

## Setup steps

1. **Create your bot**: message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`,
   follow the prompts. You'll get a bot token that looks like `123456789:AAdefwvbrf...` — this is
   a secret, treat it like a password.
2. **Find your `chat_id`** (do this before running the RTBF-Agent CLI with Telegram enabled):
   - Open a chat with your new bot in Telegram and send it any message (e.g. `/start`).
   - In a terminal, run (replace `<TOKEN>` with your bot token):
     ```bash
     curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
     ```
   - In the JSON response, find `"chat":{"id": ...}` — that number (it may be negative for a
     group chat, positive for a direct message to the bot) is your `chat_id`.
3. Set both in `.env`:
   ```bash
   TELEGRAM_BOT_TOKEN=123456789:AAdefwvbrf...
   TELEGRAM_CHAT_ID=987654321
   RTBF_NOTIFIER_BACKEND=telegram
   ```
4. Run `serve` (or `process` for a single pass) from inside the interactive shell as usual. When a
   draft is ready, you'll get a plain Telegram notification — nothing is ever sent until you
   separately record an approval with the shell's `approve-draft <case_id>` or the kanban UI.

## Answering a request for more information

If a company's reply asks for more information or an identity document, the case moves to
`Requires human action` and you'll get a plain notification message describing what's being asked
for. Record your answer from inside the shell (see the
[main guide](README.md#answering-a-sites-request-for-more-information)):

```
rtbf-agent> provide-text <case_id> "here is the information they asked for"
# or, for a document:
rtbf-agent> attach-document <case_id> /path/to/id-scan.png
```

This only *records* the answer — Telegram itself never receives it directly, and there's no
blocking wait for a reply in this chat. When you're ready, run `draft-additional-info <case_id>`
(shell) to compose a preview follow-up from it — the case stays `Requires human action` and nothing
is sent yet. Review it, then it goes through the same approval gate as any other send: nothing goes
out without a fresh `approve-draft` (shell or kanban approval).

Telegram never parses any message you send it as a decision of any kind — it has no inline buttons
and doesn't read incoming messages at all; every decision, including these answers, is recorded
through the interactive shell or kanban UI instead.

## Troubleshooting

- `LookupError: incomplete Telegram config: missing ...` — one or both of
  `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` is unset or blank.
- **Notifications never arrive** — double-check `TELEGRAM_CHAT_ID` matches the chat you intend to
  notify; a mismatch is silently dropped by design (R15), not reported as an error, since telling
  an unrecognized chat "you're not authorized" is itself information leakage.
- **You want to confirm your bot/chat id are right** — send the bot any message, then run (replace
  `<TOKEN>`):
  ```bash
  curl "https://api.telegram.org/bot<TOKEN>/getUpdates?offset=-1"
  ```
  and check the JSON response's `"chat":{"id": ...}` matches `TELEGRAM_CHAT_ID`. This is purely a
  diagnostic — the bot itself never reads incoming messages during normal operation.
