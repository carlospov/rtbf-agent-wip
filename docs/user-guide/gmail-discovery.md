# Gmail discovery setup (Google Cloud OAuth)

Scans your Gmail inbox, read-only, for account-signup/confirmation/password-recovery emails —
each distinct site found becomes a `Case`. Implemented in
`src/rtbf_agent/plugins/discovery/gmail.py`.

## Why Google Cloud is involved at all

Gmail doesn't let a third-party script log in with just a username/password — Google requires
OAuth 2.0, which means registering an OAuth client in a Google Cloud project first. This is a
one-time setup, not a recurring cost or a real "cloud service" you're standing up — it's just how
Google gates API access.

**Scope, verified in code**: this project only ever requests `gmail.readonly`
(`GMAIL_READONLY_SCOPE` in `plugins/discovery/gmail.py`) — never `gmail.modify`, `gmail.send`, or
the full-account scope. A test (`test_gmail.py`) asserts this constant's exact value, so a future
code change can't silently widen it. **Still verify this yourself** on the OAuth consent screen
before completing login — don't just take the code's word for it.

## Setup steps

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create a new project (or
   reuse an existing personal one).
2. **Enable the Gmail API**: APIs & Services → Library → search "Gmail API" → Enable.
3. **Configure the OAuth consent screen**: APIs & Services → OAuth consent screen.
   - User type: **External** (unless you have a Google Workspace org — then Internal works too).
   - App name/support email: anything, this is only shown to you.
   - Scopes: you can leave this blank here — the app requests the scope directly at login time.
   - **Publishing status: leave it in "Testing"** — no Google review needed for personal use. Add
     your own Google account as a **test user** under "Test users" (required while in Testing
     mode, or the consent screen will refuse to let you log in).
4. **Create an OAuth client ID**: APIs & Services → Credentials → Create Credentials → OAuth
   client ID → **Application type: Desktop app**. Download the resulting JSON.
5. Point your `.env` at it:
   ```bash
   RTBF_TARGET_EMAIL=you@gmail.com
   RTBF_GMAIL_CLIENT_SECRETS_FILE=/path/to/downloaded/client_secret_....json
   # RTBF_GMAIL_TOKEN_FILE optional -- defaults to ~/.config/rtbf-agent/gmail_token.json
   ```
6. First real run opens a browser for a one-time consent. **Check the consent screen shows only
   read-only Gmail access** before approving. The resulting token is cached at
   `RTBF_GMAIL_TOKEN_FILE` (or the default path above) so you won't be asked again until it
   expires — and if it does expire or gets revoked, the CLI automatically falls back to a fresh
   login rather than crashing.

## Troubleshooting

- `LookupError: no Gmail OAuth client configured` — `RTBF_GMAIL_CLIENT_SECRETS_FILE` unset or
  points at a file that doesn't exist.
- Consent screen says "app not verified" — expected in Testing mode; click "Advanced" → "Go to
  \<app name\> (unsafe)". This warning is about Google's own verification review, unrelated to
  whether the app is actually safe — it's your own OAuth client, you're the only test user.
- Login loop / "access blocked" — you likely forgot to add your own account as a test user in
  step 3.
