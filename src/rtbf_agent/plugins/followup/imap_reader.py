"""`ImapReplyReader` -- a read-only `MailboxReader` over plain IMAP (issue #102).

The IMAP-with-password sibling of `GmailReplyReader` (`plugins/followup/gmail_reader.py`), for a
user with no Gmail OAuth app -- or on a different mail provider entirely -- who still wants
follow-up reply detection (`plugins/followup/monitor.InboxReplyMonitor`). Uses stdlib `imaplib`
only: no new third-party dependency, and (unlike Gmail's OAuth) no interactive handshake is ever
needed, so `cli.process_main_async()`/`forget serve` can use this reader unattended from the very
first pass once `resolve_imap_config()` (`plugins/followup/imap_config.py`) resolves credentials --
there is no `_gmail_credentials_available_noninteractively()`-style cache-check gate for this path,
because that gate exists solely to avoid Gmail's `flow.run_local_server()` hanging with no TTY, and
a username/password IMAP login never blocks on user interaction.

Connects TLS-only via `imaplib.IMAP4_SSL` -- there is deliberately no plaintext-`imaplib.IMAP4`
option anywhere in this module or in `ImapConfig`; an IMAP password is a real credential and this
repo does not offer a config knob that would send it in the clear.

Structurally mirrors `GmailReplyReader`: it satisfies `MailboxReader`
(`plugins/followup/monitor.py`) structurally, so `InboxReplyMonitor` accepts it with no coupling to
IMAP, and it keeps the same query-scoping-as-optimization-plus-Python-side-domain-match-as-real-gate
split -- `InboxReplyMonitor` still re-applies its own authoritative sender-domain check after this
reader returns candidates (see `monitor.py`'s docstring point 1); the IMAP `SEARCH FROM` criterion
here is only a coarse, best-effort narrowing, never the real gate.

**One connection is opened lazily and reused across every `list_message_ids()`/`get_message()`
call on a given `ImapReplyReader` instance** (real report: an earlier version of this module opened
a fresh connection *per call* -- one to search, then immediately another to fetch each candidate
message. Some providers, Gmail included, can silently stall a second rapid-fire connection from the
same account rather than rejecting it outright, and `imaplib.IMAP4_SSL` sets no socket timeout by
default -- the combination hung a real monitoring pass forever, with no error, no log, nothing).
`InboxReplyMonitor` already serializes calls to a given reader behind its own `asyncio.Lock`, so
there is never a concurrency hazard to sharing one connection this way. If a command on the cached
connection fails with a transport-level error (a dropped/broken socket), `_command()` closes it,
opens a fresh one, and retries **once** -- covering the ordinary "server closed an idle connection"
case without masking a genuine, persistent failure (e.g. bad credentials), which still propagates
after the retry. Every connection attempt and command is bounded by `timeout` (`imaplib.IMAP4_SSL`
applies one socket timeout to the whole connection) so a hang, if one still happens for some other
reason, fails loud instead of blocking forever -- `run_once()`'s existing per-case `try/except`
already logs it and moves on to the next pass.

A reader instance is rebuilt fresh at the start of every `process_main_async()` pass
(`cli._build_imap_reply_monitor()`), so its cached connection's lifetime is naturally bounded to one
pass; `close()` releases it explicitly at the end of a pass if a caller wants to (best-effort, never
raises), and `__del__` calls it defensively too so a discarded reader doesn't linger holding an open
authenticated session.

Message ids are IMAP **UIDs** (`UID SEARCH`/`UID FETCH`), not sequence numbers -- sequence numbers
are only valid within one session and can shift between connections, which would break
`already_processed_ids` dedup (`monitor.py`'s heuristic point 3) across monitoring passes; UIDs are
stable for the lifetime of the mailbox.

**`list_message_ids()` only ever searches `FROM <domain>`, never `Message-ID`/`In-Reply-To`/
`References`** (real report, live-tested against a genuine Gmail IMAP account): `SEARCH HEADER
In-Reply-To <value>` never responds at all -- confirmed with a real, present header value and a
50s+ socket timeout -- and `SEARCH HEADER References <value>` silently returns zero matches, even
for a substring that's unambiguously present in a message's actual header when fetched directly.
Gmail simply doesn't index either header for IMAP search (unlike `FROM`/`SUBJECT`/`Message-ID`,
which all search normally). Precise matching against a stored outbound `Message-ID` therefore
happens client-side instead: `get_message()` parses each fetched candidate's own `References`/
`In-Reply-To` headers into `MailboxMessage.in_reply_to_ids`, and
`InboxReplyMonitor.find_new_reply()` uses that to prefer the genuine reply among whatever
`FROM <domain>` (bounded by `max_candidates`) returned -- see that class's docstring for the full
reasoning.

## `SubprocessImapReplyReader` -- the actual `MailboxReader` this codebase wires up (real report)

`ImapReplyReader`'s own connection timeout, and `monitor.InboxReplyMonitor`'s independent
`asyncio.wait_for()` backstop around every reader call, both turned out to be insufficient against a
real, reproduced hang: a live test against a genuine Gmail IMAP account froze for minutes inside a
single blocking `imaplib` call, and *neither* timeout ever fired -- confirmed by waiting well past
both. The only plausible explanation left is that whatever C-level code was blocking (`ssl`/socket
internals) was not releasing the GIL while stuck, which starves the *entire interpreter*, including
the event loop's own timer callbacks -- no thread-based timeout, however implemented, can recover
from that, because nothing in that process can run at all until the blocking call returns.

The only mechanism that is genuinely immune to this is running the blocking work in a
**separate OS process** and forcibly killing it if it overruns -- a `SIGTERM`/`terminate()` from
the parent process works regardless of what the child is stuck doing internally, since it
doesn't depend on the child's interpreter cooperating in any way. `SubprocessImapReplyReader`
(below) is a `MailboxReader` that does exactly this: `list_message_ids()` runs a full
search-then-fetch-every-candidate pass (see
`_fetch_candidates()`) in a fresh, single-use child process via `multiprocessing`, bounded by a hard
wall-clock timeout; `get_message()` is then just a lookup into what that one child process already
fetched, no further I/O. This is the reader `cli._build_imap_reply_monitor()` actually constructs;
`ImapReplyReader` itself is `_fetch_candidates()`'s implementation detail (still exercised directly
by its own tests, since forcibly killing a real subprocess mid-test is not itself something worth
testing against a fake -- see `SubprocessImapReplyReader`'s own tests for how the hard-timeout path
is proven instead, against a plain, non-IMAP-specific target function).
"""

from __future__ import annotations

import contextlib
import imaplib
import multiprocessing
from collections.abc import Sequence
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from rtbf_agent.plugins.followup.monitor import MailboxMessage

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
"""Fallback `received_at` for a message with no parseable `Date` header -- identical rationale to
`gmail_reader._EPOCH`: deliberately in the far past so such a message sorts *before* any real send
time and can never be mistaken for a post-send reply."""

_DEFAULT_MAX_CANDIDATES = 50
"""Real report: a `FROM <domain>` search can over-match badly -- e.g. a `Case.recipient_email`
recorded as the user's own mailbox address (a data bug elsewhere, not this module's) turns
"replies from the site" into "everything in my own Gmail inbox from anyone @gmail.com", matched
hundreds of messages in a real account. `list_message_ids()` fetches every candidate's full body in
one pass (`get_message()` afterward is a pure cache lookup, never touching the network again -- see
`SubprocessImapReplyReader`'s docstring), so an unbounded candidate list means an unbounded number
of full-message `UID FETCH`s -- at a fraction of a second each, a few hundred of them alone blows
through both `SubprocessImapReplyReader`'s hard timeout and `monitor.py`'s independent backstop,
turning a slow correctness bug into an apparent hang with no indication why. Capped to the most
*recent* `max_candidates` UIDs (IMAP UIDs are assigned in strictly increasing arrival order, so the
tail of a sorted-ascending search result is the newest mail) -- a real reply is essentially always
recent relative to when the request was sent, so this is a safety net against a pathologically
broad query, not a normal operating limit; it does not replace fixing an over-broad domain match at
its source (e.g. a wrong `Case.recipient_email`)."""

_DEFAULT_TIMEOUT_SECONDS = 30.0
"""Real report: `imaplib.IMAP4_SSL` sets no socket timeout by default -- a connection attempt,
login, or command that never gets a response hangs forever instead of failing. `asyncio.to_thread`
(see `monitor.py`'s `find_new_reply()`) keeps that hang off the event loop, but it still blocks the
one case's follow-up check indefinitely with no error, no log, nothing -- exactly the kind of
silent-forever-stuck state this module must not produce. A real, finite timeout turns that into a
normal exception `run_once()`'s existing per-case `try/except` already logs and moves on from."""

# Transport-level failures that mean "this connection is dead", worth one reconnect-and-retry --
# deliberately *not* `Exception` broadly, so a real protocol/logic error (e.g. a malformed command)
# doesn't get silently retried and masked as if it were a transient connection drop.
_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    imaplib.IMAP4.abort,
    OSError,
    EOFError,
)


class _Imap4SslLike(Protocol):
    """The narrow subset of `imaplib.IMAP4_SSL`'s interface this module actually uses -- lets tests
    substitute a fake connection without the real (blocking, network-touching) class."""

    def login(self, username: str, password: str) -> Any: ...

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> Any: ...

    def uid(self, command: str, *args: Any) -> Any: ...

    def logout(self) -> Any: ...


class ImapReplyReader:
    """`MailboxReader` backed by a real IMAP mailbox over TLS (read-only, password-authenticated).

    `connection_factory` defaults to `imaplib.IMAP4_SSL` (called as `connection_factory(host, port,
    timeout=timeout)`) and is only ever overridden in tests, mirroring how `GmailReplyReader`
    accepts an injected `resource` -- production code never needs to pass it explicitly.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        max_candidates: int = _DEFAULT_MAX_CANDIDATES,
        connection_factory: Any = imaplib.IMAP4_SSL,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._timeout = timeout
        self._max_candidates = max_candidates
        self._connection_factory = connection_factory
        self._conn: _Imap4SslLike | None = None

    def _open_connection(self) -> _Imap4SslLike:
        """Open a fresh TLS connection (bounded by `self._timeout`), log in, and select the inbox
        read-only. Never caches -- callers decide when to store the result on `self._conn`.

        If `login()`/`select()` fails (e.g. bad credentials), the TCP/TLS connection above them is
        already open -- logged out (best-effort) before the exception propagates, since it was never
        stored on `self._conn` for `close()`/`__del__()` to find and clean up later otherwise.
        """
        conn: _Imap4SslLike = self._connection_factory(
            self._host, self._port, timeout=self._timeout
        )
        try:
            conn.login(self._username, self._password)
            conn.select("INBOX", readonly=True)
        except Exception:
            with contextlib.suppress(Exception):
                conn.logout()
            raise
        return conn

    def _get_connection(self) -> _Imap4SslLike:
        """Return the cached connection, opening one if there isn't one yet."""
        if self._conn is None:
            self._conn = self._open_connection()
        return self._conn

    def _command(self, command: str, *args: Any) -> Any:
        """Run `self._conn.uid(command, *args)` on the cached connection, transparently
        reconnecting and retrying **once** if it fails with a transport-level error (a dropped or
        never-established connection) -- see the module docstring for why this exists. A second
        failure (including on the fresh retry connection) propagates as-is; this is a bounded retry,
        not a masking loop.
        """
        conn = self._get_connection()
        try:
            return conn.uid(command, *args)
        except _CONNECTION_ERRORS:
            self.close()
            conn = self._get_connection()
            return conn.uid(command, *args)

    def close(self) -> None:
        """Best-effort log out and drop the cached connection, if any. Never raises -- a failure
        logging out of an already-broken connection is not itself an error worth surfacing."""
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.logout()
            self._conn = None

    def __del__(self) -> None:
        # Defensive only: a reader is normally rebuilt fresh each pass and its connection released
        # explicitly, but nothing guarantees a caller does that -- this stops a discarded reader
        # from silently holding an authenticated session open indefinitely. Never let a finalizer
        # raise (Python ignores/warns on exceptions here anyway; suppressing keeps it quiet).
        with contextlib.suppress(Exception):
            self.close()

    def list_message_ids(self, query: str) -> Sequence[str]:
        """Translate `query` (Gmail-style `from:<domain>` text, per `monitor.py`) into an IMAP
        `UID SEARCH FROM <domain>` and return the matching UIDs as strings, most-recent
        `self._max_candidates` only (see that constant's docstring for why).

        This is a coarse, best-effort narrowing only -- most IMAP servers' `FROM` search does a
        substring match against the raw `From:` header, not a strict domain-only comparison, so it
        can occasionally over-match (e.g. a display name that happens to contain the domain text).
        That is fine: `InboxReplyMonitor` re-applies its own `.`-anchored sender-domain check
        authoritatively on every candidate this returns (see `monitor.py`'s docstring point 1), so
        a reader that over-matches -- or under-matches, or ignores the query outright -- is safe.
        Never searches by `Message-ID`/`In-Reply-To`/`References` -- see this module's docstring for
        why (Gmail's IMAP `SEARCH HEADER` doesn't support them); that precision instead comes from
        `get_message()` reporting each candidate's own headers for client-side matching.
        """
        domain = _domain_from_query(query)
        status, data = self._command("search", None, "FROM", f'"{domain}"')
        if status != "OK" or not data or data[0] is None:
            return []
        uids = [uid.decode("ascii") for uid in data[0].split()]
        return uids[-self._max_candidates :] if self._max_candidates > 0 else uids

    def get_message(self, message_id: str) -> MailboxMessage:
        """Fetch the full raw message for IMAP UID `message_id` and parse it into a
        `MailboxMessage`.

        `message_id` must be a UID previously returned by `list_message_ids()` on this same mailbox
        (see the module docstring for why UIDs, not session-local sequence numbers, are used).
        """
        status, data = self._command("fetch", message_id, "(RFC822)")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            raise LookupError(f"IMAP UID FETCH for message {message_id!r} returned no data")
        raw = data[0][1]
        message = message_from_bytes(raw, policy=policy.default)
        assert isinstance(message, EmailMessage)
        return MailboxMessage(
            message_id=message_id,
            from_address=message.get("From", ""),
            subject=message.get("Subject", ""),
            body=_extract_body(message),
            received_at=_parse_date(message.get("Date")),
            in_reply_to_ids=_reply_reference_ids(message),
            rfc822_message_id=message.get("Message-ID"),
        )


def _reply_reference_ids(message: EmailMessage) -> frozenset[str]:
    """Every `Message-ID` this message's `References`/`In-Reply-To` headers name, for the
    client-side thread matching `MailboxMessage.in_reply_to_ids` exists for (see this module's
    docstring: Gmail's IMAP `SEARCH HEADER` can't do this server-side). Both headers are whitespace-
    separated lists of `<local-part@domain>` tokens (RFC 5322) with no internal whitespace in a
    well-formed id, so a plain `.split()` recovers each one -- `References` is the ancestor chain
    (usually a superset of `In-Reply-To`), `In-Reply-To` is checked too since a message could carry
    one without the other."""
    ids: set[str] = set()
    for header_name in ("References", "In-Reply-To"):
        ids.update(message.get(header_name, "").split())
    return frozenset(ids)


def _domain_from_query(query: str) -> str:
    """Strip the `"from:"` prefix `monitor.py`'s `query = f"from:{expected_domain}"` always adds,
    leaving the bare domain. Defensively falls back to the whole (stripped) string if some caller
    ever passes a query without that prefix, rather than raising -- a slightly wrong IMAP `FROM`
    search criterion is harmless (see `list_message_ids()`'s docstring: the real gate is
    downstream), so there is nothing here worth failing loud over.
    """
    prefix = "from:"
    stripped = query.strip()
    if stripped.lower().startswith(prefix):
        return stripped[len(prefix) :].strip()
    return stripped


def _extract_body(message: EmailMessage) -> str:
    """Return the message's text body: `text/plain` preferred, `text/html` fallback, `""` if
    neither exists -- the same preference order `gmail_reader._extract_body()` uses.

    Built on `EmailMessage.get_body()` (available because the message was parsed with
    `email.policy.default`), which already walks the MIME tree and handles charset decoding --
    unlike Gmail's API, raw IMAP `RFC822` bodies are never pre-decoded base64, so there is no
    equivalent of `gmail_reader._decode_base64url()` needed here.
    """
    body_part = message.get_body(preferencelist=("plain", "html"))
    if body_part is None:
        return ""
    content = body_part.get_content()
    return content if isinstance(content, str) else ""


def _parse_date(raw_date: str | None) -> datetime:
    """Parse an RFC 2822 `Date` header to a timezone-aware UTC datetime, or the epoch on failure.

    Identical logic/rationale to `gmail_reader._parse_date()`: a message whose `Date` is missing or
    unparseable is dated to the far-past epoch so it can never be mistaken for a post-send reply; a
    naive parsed datetime (no offset in the header) is assumed UTC rather than silently compared
    against tz-aware send times, which would raise.
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


_DEFAULT_HARD_TIMEOUT_SECONDS = 60.0
"""Wall-clock budget for one whole `SubprocessImapReplyReader.list_message_ids()` call (a full
search-then-fetch-every-candidate pass in a fresh child process) -- deliberately looser than
`ImapReplyReader`'s own ~30s per-connection timeout, so that timeout gets the first chance to
produce a clean, specific error inside the child; this is the guarantee of last resort, enforced by
forcibly killing the child process outright if it's still not done by the time this elapses."""


def _fetch_candidates(
    host: str,
    port: int,
    username: str,
    password: str,
    timeout: float,
    max_candidates: int,
    query: str,
) -> list[MailboxMessage]:
    """Run one full `ImapReplyReader` search-then-fetch-every-candidate pass and return the results.

    Deliberately a plain, top-level, picklable function taking only plain (str/int/float) arguments
    and returning a `list` of frozen dataclasses -- everything `multiprocessing`'s `"spawn"` start
    method needs to run this in a genuinely separate process and get the result back. Opens exactly
    one `ImapReplyReader` (one connection, reused for the search and every fetch -- see that class's
    own docstring), and always closes it before returning, success or failure.
    """
    reader = ImapReplyReader(
        host=host,
        port=port,
        username=username,
        password=password,
        timeout=timeout,
        max_candidates=max_candidates,
    )
    try:
        message_ids = reader.list_message_ids(query)
        return [reader.get_message(message_id) for message_id in message_ids]
    finally:
        reader.close()


def _run_with_hard_timeout(
    func: Any, args: tuple[Any, ...], hard_timeout: float, *, mp_context: Any = None
) -> Any:
    """Run `func(*args)` in a single-use child process, forcibly terminated if it hasn't produced a
    result within `hard_timeout` seconds -- see the module docstring for why this exists (a
    thread-based timeout cannot recover from a blocking call that never releases the GIL; killing
    the OS process it runs in does, unconditionally). Raises `TimeoutError` (chained from the
    underlying `multiprocessing.TimeoutError`) if the deadline is hit; otherwise returns `func`'s
    return value, or re-raises whatever exception `func` itself raised (transparently propagated
    across the process boundary by `multiprocessing`, same as any other exception from `func`).

    `mp_context` is only ever overridden in tests (a fake standing in for `multiprocessing`'s own
    `get_context("spawn")` result, since actually spawning/killing real subprocesses in every test
    run would be slow and platform-fragile) -- production code never passes it explicitly. `"spawn"`
    (not `"fork"`) is used in production: it starts a genuinely fresh interpreter for the child
    rather than duplicating this process's memory/state, which is both the safer default across
    platforms (macOS/Windows don't reliably support `"fork"` for this) and avoids ever inheriting a
    stuck/locked state from whatever the parent process's threads happen to be doing at fork time.
    """
    ctx = mp_context if mp_context is not None else multiprocessing.get_context("spawn")
    pool = ctx.Pool(processes=1)
    try:
        async_result = pool.apply_async(func, args)
        try:
            return async_result.get(timeout=hard_timeout)
        except multiprocessing.TimeoutError as exc:
            raise TimeoutError(
                f"IMAP operation did not complete within {hard_timeout}s -- the underlying "
                "connection appears to be stuck; the process handling it has been terminated"
            ) from exc
    finally:
        # `terminate()` forcibly kills the worker regardless of what it's doing -- unlike `close()`
        # (which waits for outstanding work to finish), this is what actually recovers from a truly
        # stuck child. Safe to call even after a successful `get()`: the worker is idle by then, and
        # `terminate()` on an already-finished process is a no-op.
        pool.terminate()
        pool.join()


class SubprocessImapReplyReader:
    """`MailboxReader` backed by `ImapReplyReader`, but with every IMAP operation run inside a
    single-use, forcibly-killable child process -- see the module docstring's "why" in full.

    `list_message_ids()` runs one full search-then-fetch pass (`_fetch_candidates()`) in a fresh
    child process bounded by `hard_timeout`, and caches every returned `MailboxMessage` keyed by its
    id; `get_message()` is then a pure lookup into that cache, never touching the network itself --
    `InboxReplyMonitor` always calls `list_message_ids()` once before calling `get_message()` for
    any id it returned (see `monitor.py`'s `find_new_reply()`), so the cache is always populated by
    the time it's consulted.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        max_candidates: int = _DEFAULT_MAX_CANDIDATES,
        hard_timeout: float = _DEFAULT_HARD_TIMEOUT_SECONDS,
        mp_context: Any = None,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._timeout = timeout
        self._max_candidates = max_candidates
        self._hard_timeout = hard_timeout
        self._mp_context = mp_context
        self._cache: dict[str, MailboxMessage] = {}

    def list_message_ids(self, query: str) -> Sequence[str]:
        messages = _run_with_hard_timeout(
            _fetch_candidates,
            (
                self._host,
                self._port,
                self._username,
                self._password,
                self._timeout,
                self._max_candidates,
                query,
            ),
            self._hard_timeout,
            mp_context=self._mp_context,
        )
        self._cache = {message.message_id: message for message in messages}
        return list(self._cache)

    def get_message(self, message_id: str) -> MailboxMessage:
        return self._cache[message_id]
