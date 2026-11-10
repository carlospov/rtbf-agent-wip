# SMTP setup (outbound email)

How an approved deletion request actually gets sent — over your own email account, never a
project-operated relay. Implemented in `src/rtbf_agent/plugins/senders/smtp.py`, config resolved
by `resolve_smtp_config()` from `RTBF_SMTP_*` environment variables (or a `[smtp]` section in
`~/.config/rtbf-agent/config.toml`, if you'd rather not put credentials in `.env`).

## Using Gmail's SMTP (most common case)

Gmail requires **2-Step Verification** to be enabled on your account before it will let you
generate an **App Password** — your regular account password will not work here.

1. Enable 2-Step Verification on your Google account, if not already on:
   [myaccount.google.com/security](https://myaccount.google.com/security).
2. Generate an App Password: [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
   (search "App Passwords" in your Google Account settings if that direct link doesn't work for
   your account type). Name it anything (e.g. "rtbf-agent").
3. Configure:
   ```bash
   RTBF_SMTP_HOST=smtp.gmail.com
   RTBF_SMTP_PORT=587
   RTBF_SMTP_USE_TLS=true
   RTBF_SMTP_USERNAME=you@gmail.com
   RTBF_SMTP_PASSWORD=<the 16-character App Password, not your regular password>
   RTBF_SMTP_FROM_ADDRESS=you@gmail.com
   ```

This can be the same Gmail account as `RTBF_TARGET_EMAIL` (the one being scanned for discovery),
or a different one — nothing requires them to match.

## Any other SMTP provider

Same shape — set the four `RTBF_SMTP_*` values to whatever your provider's SMTP settings are.
`RTBF_SMTP_PORT=587` with `RTBF_SMTP_USE_TLS=true` (STARTTLS) is the most common configuration;
check your provider's docs if they specify something else (e.g. implicit TLS on port 465 is not
currently supported by this project's `SMTPSender`, which only ever calls `starttls()`).

## Troubleshooting

- `LookupError: incomplete SMTP config: missing ...` — one of `RTBF_SMTP_HOST`/
  `RTBF_SMTP_FROM_ADDRESS`/(`RTBF_SMTP_PASSWORD`, only required if `RTBF_SMTP_USERNAME` is set)
  is missing.
- **Authentication failed** — for Gmail specifically, confirm you're using an App Password, not
  your regular account password; regular passwords are rejected by Gmail's SMTP server once
  2-Step Verification is on (and Google recommends 2-Step Verification regardless).
- **Where credentials are (and aren't) stored**: nothing here is ever written to the audit log
  (`Event` rows are hash+metadata only) or logged in plaintext — `SMTPConfig.password` is
  `repr=False`'d specifically so an accidental `print`/traceback can't surface it either.
