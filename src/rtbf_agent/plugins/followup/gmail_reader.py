"""`GmailReplyReader` -- a read-only `MailboxReader` over the Gmail API (issue #68).

The production implementation of `plugins/followup/monitor.MailboxReader`, the followup analogue of
discovery's `GoogleApiGmailClient` (`plugins/discovery/gmail.py`). It is built from the *same*
read-only OAuth credentials `GmailDiscoverySource` uses -- `cli._build_gmail_reply_reader()` reuses
`cli._resolve_gmail_credentials()`, so no new Gmail scope or any write access is introduced by
follow-up monitoring. Every call here is a `list`/`get` read; nothing can send, modify, or delete
mail, matching the `GMAIL_READONLY_SCOPE` the credentials are scoped to.

Unlike discovery's `get_message_headers` (which requests only the `Subject`/`From` metadata
headers), this fetches the full message so the reply **body** is available for intent classification
-- that is the whole reason follow-up needs its own reader rather than reusing `GmailClient` (see
`monitor.py`'s docstring). `googleapiclient` is imported lazily/at the edge exactly as in
`plugins/discovery/gmail.py`, so this module stays importable (and `MailboxReader`-satisfying
tests stay runnable) without the `gmail` optional dependency installed -- tests exercise
`GmailReplyReader` against a fake resource, never a real Gmail account.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from rtbf_agent.plugins.followup.monitor import MailboxMessage

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
"""Fallback `received_at` for a message with no parseable `Date` header. Deliberately in the far
past so such a message sorts *before* any real send time and is therefore never mistaken for a
post-send reply (`InboxReplyMonitor`'s heuristic requires `received_at > sent_at`) -- a message we
can't date must never be able to masquerade as a fresh reply."""


class GmailReplyReader:
    """`MailboxReader` backed by a real `googleapiclient` Gmail API resource (read-only).

    Structurally mirrors `plugins/discovery/gmail.GoogleApiGmailClient`: it wraps the same kind of
    resource object and exposes only reads. It satisfies `MailboxReader` structurally (its two
    methods match the Protocol), so `InboxReplyMonitor` accepts it with no coupling to Gmail.
    """

    def __init__(self, resource: Any) -> None:
        self._resource = resource

    def list_message_ids(self, query: str) -> Sequence[str]:
        # Reads only the first page (no `nextPageToken` follow-up), same bounded-scan choice as
        # `GoogleApiGmailClient.list_message_ids`: Gmail returns most-recent matches first, and a
        # domain-scoped `from:<domain>` query is already narrow. A reply, if it exists, is among the
        # most recent mail from that sender.
        response = self._resource.users().messages().list(userId="me", q=query).execute()
        return [message["id"] for message in response.get("messages", [])]

    def get_message(self, message_id: str) -> MailboxMessage:
        message = (
            self._resource.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        payload = message.get("payload", {})
        headers = {header["name"].lower(): header["value"] for header in payload.get("headers", [])}
        # `references`/`in-reply-to` are already in `headers` (this reader always fetches
        # `format="full"`) -- free to expose for `InboxReplyMonitor`'s client-side reply-thread
        # matching (`MailboxMessage.in_reply_to_ids`'s docstring), same as `ImapReplyReader` does.
        in_reply_to_ids = frozenset(
            headers.get("references", "").split() + headers.get("in-reply-to", "").split()
        )
        return MailboxMessage(
            message_id=message_id,
            from_address=headers.get("from", ""),
            subject=headers.get("subject", ""),
            body=_extract_body(payload),
            received_at=_parse_date(headers.get("date")),
            in_reply_to_ids=in_reply_to_ids,
            rfc822_message_id=headers.get("message-id"),
        )


def _parse_date(raw_date: str | None) -> datetime:
    """Parse an RFC 2822 `Date` header to a timezone-aware UTC datetime, or the epoch on failure.

    A message whose `Date` is missing or unparseable is dated to the far-past epoch (see `_EPOCH`)
    so it can never be mistaken for a post-send reply; a naive parsed datetime (no offset in the
    header) is assumed UTC rather than silently compared against tz-aware send times, which would
    raise.
    """
    if not raw_date:
        return _EPOCH
    try:
        parsed = parsedate_to_datetime(raw_date)
    except (TypeError, ValueError):
        return _EPOCH
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _extract_body(payload: dict[str, Any]) -> str:
    """Return the message's plain-text body, walking a MIME tree for the first `text/plain` part.

    Prefers `text/plain`; falls back to the first `text/html` part (tags and all -- an imperfect but
    non-empty signal for classification is better than dropping an HTML-only reply entirely) only
    when no plain-text part exists; returns `""` if neither is present. Gmail encodes part bodies as
    URL-safe base64, decoded here with `validate=False`-equivalent tolerance (`+ padding`) so a
    correctly-formed part is never lost to a padding quirk.
    """
    plain = _find_part_data(payload, "text/plain")
    if plain is not None:
        return plain
    html = _find_part_data(payload, "text/html")
    return html if html is not None else ""


def _find_part_data(part: dict[str, Any], mime_type: str) -> str | None:
    """Depth-first search a Gmail payload tree for the first `mime_type` part, decoded to text."""
    if part.get("mimeType") == mime_type:
        data = part.get("body", {}).get("data")
        if data:
            return _decode_base64url(data)
    for sub_part in part.get("parts", []) or []:
        found = _find_part_data(sub_part, mime_type)
        if found is not None:
            return found
    return None


def _decode_base64url(data: str) -> str:
    """Decode Gmail's URL-safe base64 body data to text, re-padding first so length is a multiple
    of 4 (Gmail strips `=` padding), and replacing undecodable bytes rather than raising.

    A malformed/non-base64 `data` value from the API (unexpected, but not something this module
    should ever crash a monitoring pass over) yields `""` rather than propagating -- a body this
    module can't decode is no worse to `ResponseClassifier` than a message with no body at all.
    """
    padded = data + "=" * (-len(data) % 4)
    try:
        # binascii.Error (raised on invalid base64 input) has been a ValueError subclass since
        # early Python 3 (verified against this project's actual supported/CI interpreters, not
        # just assumed) -- `except ValueError` here is not missing a case for `binascii.Error`.
        # A plain `str` argument (not `bytes`) is also explicitly supported by
        # `base64.urlsafe_b64decode` (verified directly, not assumed) -- no `.encode()` needed.
        decoded = base64.urlsafe_b64decode(padded)
    except ValueError:
        return ""
    return decoded.decode("utf-8", errors="replace")
