"""`InboxReplyMonitor` -- find the reply (if any) a site sent back to a `Case`'s deletion request.

The monitoring half of the "Followup" component (issue #68, spec §3.1). Given a `Case` that has
been `Sent`, it looks in the user's own mailbox for a *new* message that looks like a reply to that
specific request, so `ResponseClassifier` (this package's `detector.py`) can then read its intent.

## Why not reuse `GmailDiscoverySource` directly

Issue #68 asks to "reuse `GmailDiscoverySource`'s existing read access". `GmailDiscoverySource`
itself is the wrong *unit* to reuse for replies, for two concrete reasons:

- It only surfaces mail whose **subject** matches account-lifecycle patterns (welcome/confirm/
  verify/reset/created -- `plugins/discovery/gmail._SUBJECT_PATTERNS`). A reply to a deletion
  request ("We have deleted your account") matches none of those, so `discover()` would simply not
  return it.
- Its `GmailClient` seam exposes only `Subject`/`From` *headers* (`get_message_headers`), never a
  message **body** -- but classifying a reply's intent needs the body text.

So what this module reuses is the *read access itself*: the same narrow, read-only mailbox seam
(`MailboxReader` below, the followup analogue of discovery's `GmailClient` Protocol) and the same
`from:<domain>` Gmail-search query-scoping idiom that `cli._build_gmail_registration_check_source()`
(issue #66) already established -- just pointed at a reader that also returns the body. The
production `MailboxReader` (`plugins/followup/gmail_reader.GmailReplyReader`) is built from the
exact same read-only OAuth credentials as `GmailDiscoverySource`; no new Gmail scope or write
access is introduced.

## The reply-matching heuristic (documented deliberately -- issue #68)

A candidate message is treated as a reply to `case` iff **all** of:

1. **Sender domain matches the case.** The message's `From:` address is at `case`'s expected reply
   domain -- the domain of `case.recipient_email` (where the request was actually sent), falling
   back to `case.domain` when no recipient is recorded. "Matches" means the sender domain equals the
   expected domain *or* is a subdomain of it (`.`-anchored, so `mail.example.com` matches
   `example.com` but a look-alike `notexample.com`/`example.com.evil.com` never does -- the same
   anchoring `_cross_check_registration()` uses). This is deliberately *not* a full thread/`In-
   Reply-To` header match: a company very often replies from a different mailbox (a ticketing
   system, `support@`) than the `privacy@` alias the request was sent to, so a strict header/thread
   match would miss real replies. Scoping to the sender's registrable domain is the tightest filter
   that still catches those, and the Gmail-side `from:<domain>` query narrows the fetch before this
   Python-side check re-applies it authoritatively (the query is an optimization; the domain match
   here is the real gate, so a reader that ignores the query -- e.g. a test fake -- is still safe).

2. **Arrived after the request was sent.** The message's received time is strictly after
   `sent_at` (the timestamp of the `Case`'s `request_sent` `Event`). This excludes the original
   signup/confirmation mail from the same domain that discovery found *before* the request was ever
   sent -- without it, that pre-existing mail would be misread as a "reply".

3. **Not already processed.** The message id is not in `already_processed_ids` (the set of message
   ids the follow-up loop has already classified for this case -- see
   `rtbf_agent.followup._processed_reply_message_ids()`). This is how the same reply is never
   classified twice across repeated monitoring passes: every classified message's id is recorded on
   its `response_classified` `Event`, and a later pass skips any id already there.

When several messages qualify, one whose own `References`/`In-Reply-To` headers name the outbound
`Message-ID` we sent (`MailboxMessage.in_reply_to_ids`, when the case has one recorded and a
qualifying candidate carries it) is preferred; otherwise, and always among ties, the **oldest**
(earliest `received_at`) is returned, so replies are processed in the order they arrived, one per
monitoring pass. This preference exists precisely because domain-matching alone (point 1) can be
spuriously true -- most sharply when `Case.recipient_email` is wrong and happens to be the user's
own mailbox address, where *every* self-received message satisfies point 1 -- see
`find_new_reply()`'s own docstring for the full reasoning and why this is a selection preference,
never an additional required gate. `find_new_reply()` returns `None` -- never raises -- when nothing
qualifies; that `None` is exactly what lets a `Case` with no reply yet be left completely untouched
(R9): this module proposes nothing, transitions nothing, and implies no deadline. It only ever
*reports* a reply that genuinely exists.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from email.utils import parseaddr
from typing import Protocol, runtime_checkable

from rtbf_agent.core.models import Case


@dataclass(frozen=True)
class MailboxMessage:
    """One fetched mailbox message -- enough for reply-matching and intent classification.

    Deliberately carries the message body (unlike discovery's header-only `GmailClient`), because
    classifying a reply's intent needs the actual text. Frozen so a matched message can't be mutated
    out from under the classification/audit that follows.
    """

    message_id: str
    """The mailbox-native, stable id of the message -- recorded on the `response_classified` `Event`
    so a later pass can tell this message has already been handled (the dedup key, see the module
    docstring's heuristic point 3)."""
    from_address: str
    """The raw `From:` header value (may be `"Name <addr@host>"`); parsed for its domain here."""
    subject: str
    body: str
    received_at: datetime
    """When the message arrived, timezone-aware UTC. Compared against the `request_sent` timestamp
    so mail that predates the send is never misread as a reply (heuristic point 2)."""
    in_reply_to_ids: frozenset[str] = frozenset()
    """Every `Message-ID` this message's own `References`/`In-Reply-To` headers name (real report:
    IMAP `SEARCH HEADER` on `References`/`In-Reply-To` cannot be used to find this server-side --
    live-tested against a genuine Gmail IMAP account: `HEADER In-Reply-To` never responds at all,
    and `HEADER References` silently returns zero matches even for an exact, present value -- Gmail
    simply doesn't index either header for IMAP search, unlike `FROM`/`SUBJECT`/`Message-ID`. So a
    reader fetches candidates the way it already reliably can (`FROM <domain>`, bounded by
    `max_candidates`) and reports what each candidate's *own* headers actually say here; matching
    against a stored outbound `Message-ID` then happens client-side, in
    `InboxReplyMonitor.find_new_reply()` (see the module docstring's heuristic). Empty for a reader
    that doesn't parse
    these headers (e.g. a test fake) -- safe: it just never wins the header-match preference below,
    same as any other candidate that isn't a reply to what we sent."""
    rfc822_message_id: str | None = None
    """This message's own `Message-ID` header value, or `None` if a reader doesn't parse it.

    Distinct from both other id-shaped fields on this class: `message_id` above is the
    mailbox-native id (an IMAP UID or Gmail message id, used for dedup -- never meaningful outside
    this mailbox); `in_reply_to_ids` is what *this* message's headers point *at* (used to match it
    against an outbound `Message-ID` we're looking for). This is the reverse direction -- what a
    *later* reply from us should cite in *our* outgoing `In-Reply-To`/`References` to thread back
    to this exact message (`agent_loop._execute_send()`'s job, once it's stored on the
    `additional_info_requested` `Event` that follow-up records for this reply -- see
    `followup.follow_up()`)."""


@runtime_checkable
class MailboxReader(Protocol):
    """The narrow, read-only mailbox surface `InboxReplyMonitor` depends on -- two methods.

    The followup analogue of discovery's `GmailClient` Protocol (`plugins/discovery/gmail.py`): a
    seam tests satisfy with a fake, and production satisfies with `GmailReplyReader`
    (`plugins/followup/gmail_reader.py`) over the same read-only Gmail credentials. Read-only by
    construction -- it only lists and gets messages; nothing here can send, modify, or delete mail.
    """

    def list_message_ids(self, query: str) -> Sequence[str]:
        """Return the ids of messages matching `query` (a mailbox search query).

        `query` is always `from:<domain>` (Gmail-search-operator style) -- a coarse, best-effort
        narrowing only; see this module's docstring point 1 for why the real gate is always the
        Python-side domain check, never this query. `find_new_reply()` does *not* ask a reader to
        search by `Message-ID`/`In-Reply-To`/`References` here (real report: live-tested against a
        genuine Gmail IMAP account, `SEARCH HEADER` on those fields either never responds at all or
        silently matches nothing, even for a value that's unambiguously present -- Gmail simply
        doesn't index them for IMAP search). Precise reply matching against a stored outbound
        `Message-ID` happens client-side instead, from what `get_message()` reports on each fetched
        candidate (`MailboxMessage.in_reply_to_ids`) -- see `find_new_reply()`'s docstring."""
        ...

    def get_message(self, message_id: str) -> MailboxMessage:
        """Return the full `MailboxMessage` (headers + body + received time) for `message_id`."""
        ...


def _address_domain(raw_from: str) -> str | None:
    """The lowercased domain of the address in a `From:` header, or `None` if it carries none."""
    _, address = parseaddr(raw_from)
    if "@" not in address:
        return None
    domain = address.rsplit("@", 1)[-1].strip().lower()
    return domain or None


def _expected_reply_domain(case: Case) -> str | None:
    """The domain a reply to `case` is expected from: its recipient's, else the case's own domain.

    A `Case` that has actually been sent always has `recipient_email` set (the send-path guards
    refuse otherwise -- `agent_loop._execute_send()`), so in practice this is the recipient's
    domain; `case.domain` is a defensive fallback that keeps the matcher usable even for a case
    whose recipient somehow isn't recorded, rather than returning `None` and matching nothing.

    Parses `recipient_email` with `parseaddr` (mirroring `_address_domain()` below), rather than a
    raw `rsplit("@", 1)`: `case_actions.confirm_recipient()` only validates that the typed value
    contains `"@"`, not that it's a bare address with no display name, so a value like `"Privacy
    Team <privacy@company.com>"` can genuinely reach this field. A raw `rsplit` would extract
    `"company.com>"` (with a trailing `>`) from that, which would never match any real sender
    domain -- silently breaking reply monitoring for that case with no error anywhere.
    """
    if case.recipient_email and "@" in case.recipient_email:
        _, address = parseaddr(case.recipient_email)
        if "@" in address:
            domain = address.rsplit("@", 1)[-1].strip().lower()
            if domain:
                return domain
    domain = case.domain.strip().lower()
    return domain or None


def _sender_matches(sender_domain: str, expected_domain: str) -> bool:
    """Whether `sender_domain` is `expected_domain` or a `.`-anchored subdomain of it.

    The `.`-anchored suffix check (`.expected`, never a bare `endswith`) matches `mail.example.com`
    for `example.com` but never a look-alike like `notexample.com` or `example.com.evil.com` -- the
    same anchoring `cli._cross_check_registration()` uses so a loose `from:` query can't smuggle in
    a foreign sender.
    """
    return sender_domain == expected_domain or sender_domain.endswith(f".{expected_domain}")


_READER_CALL_TIMEOUT_SECONDS = 45.0
"""Real report: a real `MailboxReader` (`ImapReplyReader`, over a genuine Gmail IMAP account) hung
completely -- for minutes, observed directly -- inside a single blocking `get_message()` call,
even though that reader has its own internal socket-level timeout. Whatever the exact cause (a
Python/ssl/imaplib-stack subtlety where a socket's configured timeout doesn't end up bounding
every blocking read on it), a reader-implementation-level timeout cannot be fully trusted to
always fire. This is a second, independent backstop at the `asyncio` layer, applied uniformly to
*every* `MailboxReader` implementation (not just IMAP's): `asyncio.wait_for()` bounds the `await`
itself, so a monitoring pass gives up after this many seconds regardless of what the underlying
blocking call is actually doing. Deliberately looser than `ImapReplyReader`'s own ~30s connection
timeout, so that timeout gets the first chance to fire with a clean, specific error; this is the
guarantee of last resort. Note this cannot forcibly stop the underlying OS thread
`asyncio.to_thread()` runs the blocking call on (Python threads cannot be killed) -- a genuinely
stuck call leaks one worker thread and one dangling connection rather than truly terminating, but
critically the *pass* is no longer blocked forever: the `TimeoutError` this raises propagates to
`run_once()`'s existing per-case `try/except`, which logs it and moves on, exactly as it already
does for any other collaborator failure (R9)."""


class InboxReplyMonitor:
    """Finds the oldest new reply to a `Case`, over an injected read-only `MailboxReader`.

    The reader is injected (not constructed here), mirroring every other capability in this repo --
    so this class is exercised in tests against a fake reader without a real Gmail account, and
    swapping the mailbox backend is a wiring change at the call site, never a change here (R21).
    Never mutates any `Case`/`Event` state itself and never raises on "nothing found": recording
    what it finds, and applying any resulting (human-confirmed) transition, is
    `rtbf_agent.followup`'s job, exactly as `DiscoverySource.discover()` leaves state changes to its
    orchestrator (R1, R3).
    """

    def __init__(self, reader: MailboxReader) -> None:
        self._reader = reader
        # The production reader wraps a `googleapiclient` transport that is not thread-safe when
        # shared across threads (same rationale as `GmailDiscoverySource`'s lock). Each
        # `asyncio.to_thread` call below may run on a different worker thread, so serialize a given
        # instance's `find_new_reply` calls -- concurrent callers queue instead of racing on the
        # shared client.
        self._lock = asyncio.Lock()

    async def find_new_reply(
        self,
        case: Case,
        *,
        sent_at: datetime,
        already_processed_ids: frozenset[str],
        in_reply_to_message_id: str | None = None,
    ) -> MailboxMessage | None:
        """Return the oldest unprocessed reply to `case` arriving after `sent_at`, or `None`.

        `None` means "no reply to act on" -- the case must then be left completely untouched (R9).
        See the module docstring for the full matching heuristic and why each clause is there.

        `in_reply_to_message_id`, when given, is the outbound `Message-ID` recorded on this case's
        `request_sent` `Event` (`followup._latest_request_sent_event()`). It does *not* narrow the
        mailbox search (see `MailboxReader.list_message_ids()`'s docstring for why a header-based
        server-side search isn't reliable on Gmail); instead, once candidates are fetched, a
        candidate whose own `in_reply_to_ids` contains this value is preferred over one that doesn't
        -- a much stronger signal than "arrived after send from the right domain" alone, since a
        domain that matches spuriously (e.g. `Case.recipient_email` accidentally recorded as the
        user's own mailbox address, so *every* self-received message technically matches "the right
        domain") can otherwise pick an unrelated message as "the reply". `None` (every case sent
        before this existed, or a `Sender` that never generated one, or no fetched candidate's
        headers reference it) falls back to the plain "oldest matching candidate" rule exactly as
        before -- this is a selection *preference*, never an additional required gate, so it can
        only ever narrow which valid candidate wins, never accept one that fails the domain/time/
        dedup checks below.
        """
        expected_domain = _expected_reply_domain(case)
        if expected_domain is None:
            return None

        async with self._lock:
            query = f"from:{expected_domain}"
            message_ids = await asyncio.wait_for(
                asyncio.to_thread(self._reader.list_message_ids, query),
                timeout=_READER_CALL_TIMEOUT_SECONDS,
            )

            candidates: list[MailboxMessage] = []
            for message_id in message_ids:
                if message_id in already_processed_ids:
                    continue
                message = await asyncio.wait_for(
                    asyncio.to_thread(self._reader.get_message, message_id),
                    timeout=_READER_CALL_TIMEOUT_SECONDS,
                )
                sender_domain = _address_domain(message.from_address)
                if sender_domain is None or not _sender_matches(sender_domain, expected_domain):
                    continue
                if message.received_at <= sent_at:
                    continue
                candidates.append(message)

            if not candidates:
                return None
            if in_reply_to_message_id:
                referencing = [
                    message
                    for message in candidates
                    if in_reply_to_message_id in message.in_reply_to_ids
                ]
                if referencing:
                    return min(referencing, key=lambda message: message.received_at)
            return min(candidates, key=lambda message: message.received_at)
