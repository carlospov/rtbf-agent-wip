# Sensitive document uploads (identity documents)

If a company's reply asks you to prove your identity before it'll process a deletion request —
usually a passport/ID scan — this is how you provide it. Covers the encrypted-storage lifecycle
and the interactive shell's `attach-document` command that records it.

Implemented in `src/rtbf_agent/document_store.py` (storage/lifecycle) and
`src/rtbf_agent/case_actions.py` (the non-blocking decision-recording functions the interactive
shell and kanban UI both call).

## Setup: the encryption key

Required before any document can be stored — without it, a document upload fails loud rather than
falling back to storing it unencrypted. Generate one and set it in `.env`:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```bash
RTBF_DOCUMENT_ENCRYPTION_KEY=<paste the generated key here>
# Optional -- defaults to ~/.local/share/rtbf-agent/documents (created with 0700 permissions)
RTBF_DOCUMENT_STORAGE_DIR=
```

Treat `RTBF_DOCUMENT_ENCRYPTION_KEY` like any other secret: never commit it, and losing it means
losing access to anything already encrypted with it (there's no recovery path — by design, this
project never keeps a second copy of your data anywhere).

## What actually happens to a document you provide

1. A company's reply asks for more information/an ID document; the case moves to
   `Requires human action` and you're notified what's being asked for (a plain, one-way
   notification — nothing waits on you to respond in real time).
2. Whenever you get to it, run from inside the interactive shell (see the main
   [user guide](README.md#answering-a-sites-request-for-more-information)):
   ```
   rtbf-agent> attach-document <case_id> /path/to/id-scan.png
   ```
3. The document is encrypted immediately and stored under `RTBF_DOCUMENT_STORAGE_DIR` — never
   written to disk in plaintext at any point. This only *records* the document against the case;
   nothing is drafted or sent yet.
4. Run `draft-additional-info <case_id>` (shell) to compose a preview follow-up draft noting
   the document is attached — still just a preview, the case stays `Requires human action` and
   nothing is sent yet. Review it, then it goes through the exact same approval gate as any other
   send: nothing is attached or sent without a fresh `approve-draft` (kanban or shell) — the
   original send's approval is never reused for this one.
5. Only once that send actually succeeds is the document decrypted (in memory only), attached to
   the outgoing email, and then **securely deleted** — overwritten on disk before being unlinked,
   not just logically removed.
6. If you never approve it, it's purged automatically after **24 hours** (not currently
   configurable) — securely deleted the same way, and you'd need to re-upload if you still need to
   provide it. Not accumulating an unapproved sensitive file indefinitely is treated as more
   important than the convenience of a longer window.

The audit trail (visible on each case's kanban detail page) only ever records a document's hash
and size for these steps — never its content.

## Known limitations

- **Telegram-transit limitation**: once a file you upload passes through Telegram's own Bot API,
  what happens to it there is outside this project's control — a platform limitation, not
  something the encrypted-storage/secure-delete lifecycle above can guarantee against.
- **Secure delete isn't an absolute guarantee**: overwriting a file before unlinking it defeats a
  plain-recovery attempt, but modern filesystems (SSD wear-leveling, copy-on-write filesystems like
  btrfs/ZFS) can retain a copy of the old data elsewhere on disk regardless. Best-effort, not a
  cryptographic guarantee — the actual protection against a document lingering readable at rest is
  the encryption, not the delete step alone.
- **The 24-hour TTL purge is a primitive, not yet wired into an automatic background sweep** — the
  code to purge an expired, unapproved document exists and is tested, but nothing currently calls
  it automatically on a schedule. In practice this means an unapproved document may sit encrypted
  on disk past 24 hours until something triggers a purge pass; it is still never plaintext, and
  never sent without your approval either way.

## Troubleshooting

- **`ValueError: RTBF_DOCUMENT_ENCRYPTION_KEY is not set`** — generate and set one, see Setup above.
- **A previously-working key stops decrypting anything** — `RTBF_DOCUMENT_ENCRYPTION_KEY` changed
  (or was regenerated) since a document was stored; there is no way to recover a document encrypted
  under a key you no longer have. Re-upload if still needed.
