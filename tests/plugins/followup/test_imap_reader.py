"""Tests for `ImapReplyReader` (issue #102): a read-only `MailboxReader` over plain IMAP.

Exercised against a fake `imaplib.IMAP4_SSL`-shaped connection -- never a real IMAP server or
network call, exactly as `tests/plugins/followup/test_gmail_reader.py` fakes the Gmail API resource.
"""

from __future__ import annotations

import imaplib
import time
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

from rtbf_agent.plugins.followup.imap_reader import ImapReplyReader
from rtbf_agent.plugins.followup.monitor import MailboxMessage, MailboxReader


def _plain_raw(
    *,
    body: str = "We have deleted your account.",
    date: str = "Fri, 10 Jul 2026 14:30:00 +0000",
    from_address: str = "Support <support@example-corp.com>",
    references: str | None = None,
    in_reply_to: str | None = None,
    message_id: str | None = None,
) -> bytes:
    extra_headers = ""
    if references is not None:
        extra_headers += f"References: {references}\r\n"
    if in_reply_to is not None:
        extra_headers += f"In-Reply-To: {in_reply_to}\r\n"
    if message_id is not None:
        extra_headers += f"Message-ID: {message_id}\r\n"
    return (
        f"From: {from_address}\r\n"
        "Subject: Re: deletion request\r\n"
        f"Date: {date}\r\n"
        f"{extra_headers}"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        f"{body}\r\n"
    ).encode()


def _multipart_raw(*, plain: str | None, html: str | None) -> bytes:
    message = MIMEMultipart("alternative")
    message["From"] = "Support <support@example-corp.com>"
    message["Subject"] = "Re: deletion request"
    message["Date"] = "Fri, 10 Jul 2026 14:30:00 +0000"
    if plain is not None:
        message.attach(MIMEText(plain, "plain"))
    if html is not None:
        message.attach(MIMEText(html, "html"))
    return message.as_bytes()


class _FakeImapConnection:
    """A fake standing in for `imaplib.IMAP4_SSL`'s narrow subset this module uses."""

    def __init__(
        self,
        *,
        search_ids: bytes = b"",
        messages: dict[str, bytes] | None = None,
        search_status: str = "OK",
        fetch_status: str = "OK",
        raise_on_select: Exception | None = None,
        raise_on_uid: Exception | None = None,
    ) -> None:
        self.search_ids = search_ids
        self.messages = messages or {}
        self.search_status = search_status
        self.fetch_status = fetch_status
        self._raise_on_select = raise_on_select
        self._raise_on_uid = raise_on_uid
        self.logged_in_with: tuple[str, str] | None = None
        self.logged_out = False
        self.selected: tuple[str, bool] | None = None
        self.last_search_criteria: tuple[str, ...] | None = None

    def login(self, username: str, password: str):
        self.logged_in_with = (username, password)
        return ("OK", [b"Logged in"])

    def select(self, mailbox: str = "INBOX", readonly: bool = False):
        if self._raise_on_select is not None:
            raise self._raise_on_select
        self.selected = (mailbox, readonly)
        return ("OK", [b"1"])

    def uid(self, command: str, *args: str):
        if self._raise_on_uid is not None:
            raise self._raise_on_uid
        if command == "search":
            self.last_search_criteria = args
            return (self.search_status, [self.search_ids])
        if command == "fetch":
            message_id = args[0]
            if self.fetch_status != "OK":
                return (self.fetch_status, [None])
            raw = self.messages.get(message_id)
            if raw is None:
                return ("OK", [None])
            return ("OK", [(f"{message_id} (RFC822 {{{len(raw)}}}".encode(), raw), b")"])
        raise AssertionError(f"unexpected uid command {command!r}")

    def logout(self):
        self.logged_out = True
        return ("BYE", [b"Logging out"])


class _RecordingFactory:
    """A `connection_factory` that records every connection it creates and returns connections
    from a queue -- lets tests assert exactly how many times (re)connection happened."""

    def __init__(self, *connections: _FakeImapConnection) -> None:
        self._connections = list(connections)
        self.calls: list[tuple[str, int, float | None]] = []

    def __call__(self, host: str, port: int, timeout: float | None = None):
        self.calls.append((host, port, timeout))
        return self._connections[len(self.calls) - 1]


def _reader(
    connection: _FakeImapConnection, *, timeout: float = 30.0
) -> tuple[ImapReplyReader, _RecordingFactory]:
    factory = _RecordingFactory(connection)
    reader = ImapReplyReader(
        host="imap.example.com",
        port=993,
        username="carlos",
        password="sk-test-password",
        timeout=timeout,
        connection_factory=factory,
    )
    return reader, factory


def test_imap_reply_reader_satisfies_mailbox_reader_protocol() -> None:
    reader, _ = _reader(_FakeImapConnection())
    assert isinstance(reader, MailboxReader)


def test_list_message_ids_returns_ids_and_scopes_the_query() -> None:
    connection = _FakeImapConnection(search_ids=b"1 2 3")
    reader, _ = _reader(connection)

    assert list(reader.list_message_ids("from:example-corp.com")) == ["1", "2", "3"]
    # First positional arg is the IMAP SEARCH command's CHARSET slot (None = default/US-ASCII) --
    # the same `M.uid('search', None, 'FROM', ...)` shape imaplib's own docs/examples use.
    assert connection.last_search_criteria == (None, "FROM", '"example-corp.com"')


def test_list_message_ids_logs_in_and_selects_inbox_read_only() -> None:
    connection = _FakeImapConnection(search_ids=b"")
    reader, _ = _reader(connection)

    reader.list_message_ids("from:example-corp.com")

    assert connection.logged_in_with == ("carlos", "sk-test-password")
    assert connection.selected == ("INBOX", True)


def test_connection_is_opened_once_and_reused_across_calls() -> None:
    """Real report: an earlier version opened a fresh connection for every single call (one to
    search, then immediately another to fetch) -- some providers silently stall a second rapid-fire
    connection from the same account, which hung a real monitoring pass forever. One connection must
    now be opened lazily and reused for every subsequent call on the same reader."""
    connection = _FakeImapConnection(search_ids=b"1", messages={"1": _plain_raw()})
    reader, factory = _reader(connection)

    reader.list_message_ids("from:example-corp.com")
    reader.get_message("1")
    reader.list_message_ids("from:example-corp.com")

    assert len(factory.calls) == 1
    assert connection.logged_out is False  # never closed between calls


def test_open_connection_logs_out_a_partially_opened_connection_on_login_or_select_failure() -> (
    None
):
    """Real report: if `login()`/`select()` raised (e.g. bad credentials), the TCP/TLS connection
    already opened above them was never logged out or stored anywhere `close()`/`__del__()` could
    later find it -- silently leaking an open (possibly authenticated) session on the server across
    repeated failures. `_open_connection()` must clean up before propagating the failure."""
    connection = _FakeImapConnection(raise_on_select=imaplib.IMAP4.error("bad credentials"))
    reader, _ = _reader(connection)

    with pytest.raises(imaplib.IMAP4.error):
        reader.list_message_ids("from:example-corp.com")

    assert connection.logged_out is True


def test_connection_factory_receives_the_timeout() -> None:
    connection = _FakeImapConnection(search_ids=b"")
    reader, factory = _reader(connection, timeout=45.0)

    reader.list_message_ids("from:example-corp.com")

    assert factory.calls == [("imap.example.com", 993, 45.0)]


def test_list_message_ids_returns_empty_when_search_fails() -> None:
    connection = _FakeImapConnection(search_ids=b"", search_status="NO")
    reader, _ = _reader(connection)

    assert reader.list_message_ids("from:example-corp.com") == []


def test_list_message_ids_returns_empty_list_for_no_matches() -> None:
    connection = _FakeImapConnection(search_ids=b"")
    reader, _ = _reader(connection)

    assert reader.list_message_ids("from:example-corp.com") == []


def test_list_message_ids_caps_to_the_most_recent_max_candidates() -> None:
    """Real report: a `FROM <domain>` search can badly over-match (e.g. a wrong
    `Case.recipient_email` turning "replies from the site" into "everything in my own inbox from
    that domain"), and every returned id gets its full body fetched next -- hundreds of full
    fetches for one pass is what actually produced a real, reproduced multi-minute hang, not a
    genuine network/timeout failure. `max_candidates` caps that. IMAP UIDs increase in strictly
    ascending arrival order, so the tail of the (ascending) search result is the newest mail --
    this asserts the *last* ids survive the cap, not an arbitrary prefix."""
    connection = _FakeImapConnection(search_ids=b"1 2 3 4 5")
    factory = _RecordingFactory(connection)
    reader = ImapReplyReader(
        host="imap.example.com",
        port=993,
        username="carlos",
        password="sk-test-password",
        max_candidates=2,
        connection_factory=factory,
    )

    assert list(reader.list_message_ids("from:example-corp.com")) == ["4", "5"]


def test_list_message_ids_max_candidates_zero_means_unbounded() -> None:
    connection = _FakeImapConnection(search_ids=b"1 2 3")
    factory = _RecordingFactory(connection)
    reader = ImapReplyReader(
        host="imap.example.com",
        port=993,
        username="carlos",
        password="sk-test-password",
        max_candidates=0,
        connection_factory=factory,
    )

    assert list(reader.list_message_ids("from:example-corp.com")) == ["1", "2", "3"]


def test_get_message_extracts_headers_body_and_date() -> None:
    connection = _FakeImapConnection(messages={"1": _plain_raw()})
    reader, _ = _reader(connection)

    message = reader.get_message("1")

    assert message.message_id == "1"
    assert message.from_address == "Support <support@example-corp.com>"
    assert message.subject == "Re: deletion request"
    # `.strip()`: `EmailMessage.get_content()` preserves the body's trailing line terminator,
    # which carries no meaning for `ResponseClassifier`'s intent classification.
    assert message.body.strip() == "We have deleted your account."
    assert message.received_at == datetime(2026, 7, 10, 14, 30, tzinfo=UTC)
    assert message.in_reply_to_ids == frozenset()
    assert message.rfc822_message_id is None


def test_get_message_parses_the_messages_own_message_id() -> None:
    """Real report: without this, a later resend threading back to this exact message
    (`agent_loop._thread_reference_ids()`) has nothing to put in `In-Reply-To`/`References`."""
    raw = _plain_raw(message_id="<reply-from-site@example-corp.com>")
    connection = _FakeImapConnection(messages={"1": raw})
    reader, _ = _reader(connection)

    assert reader.get_message("1").rfc822_message_id == "<reply-from-site@example-corp.com>"


def test_get_message_parses_references_and_in_reply_to_into_in_reply_to_ids() -> None:
    """Real report: Gmail's IMAP `SEARCH HEADER` can't find a reply by these headers server-side
    (see this module's docstring) -- `InboxReplyMonitor` matches client-side instead, off exactly
    this field."""
    raw = _plain_raw(
        references="<orig@example.com> <also-orig@example.com>",
        in_reply_to="<orig@example.com>",
    )
    connection = _FakeImapConnection(messages={"1": raw})
    reader, _ = _reader(connection)

    message = reader.get_message("1")

    assert message.in_reply_to_ids == {"<orig@example.com>", "<also-orig@example.com>"}


def test_get_message_prefers_text_plain_within_a_multipart_tree() -> None:
    raw = _multipart_raw(plain="plain version", html="<p>html version</p>")
    connection = _FakeImapConnection(messages={"1": raw})
    reader, _ = _reader(connection)

    assert reader.get_message("1").body == "plain version"


def test_get_message_falls_back_to_html_when_no_plain_part() -> None:
    raw = _multipart_raw(plain=None, html="<p>only html</p>")
    connection = _FakeImapConnection(messages={"1": raw})
    reader, _ = _reader(connection)

    assert "only html" in reader.get_message("1").body


def test_get_message_with_unparseable_date_sorts_before_any_send_time() -> None:
    raw = _plain_raw(date="not a date")
    connection = _FakeImapConnection(messages={"1": raw})
    reader, _ = _reader(connection)

    message = reader.get_message("1")
    # Epoch -> strictly before any realistic send time, so a dateless message can never masquerade
    # as a fresh post-send reply.
    assert message.received_at < datetime(2000, 1, 1, tzinfo=UTC)


def test_get_message_assumes_utc_for_a_date_without_an_offset() -> None:
    raw = _plain_raw(date="Fri, 10 Jul 2026 14:30:00")
    connection = _FakeImapConnection(messages={"1": raw})
    reader, _ = _reader(connection)

    message = reader.get_message("1")
    assert message.received_at.tzinfo is not None
    assert message.received_at == datetime(2026, 7, 10, 14, 30, tzinfo=UTC)


def test_get_message_raises_lookuperror_when_fetch_returns_no_data() -> None:
    connection = _FakeImapConnection(messages={})
    reader, _ = _reader(connection)

    with pytest.raises(LookupError, match="1"):
        reader.get_message("1")


def test_close_logs_out_and_clears_the_cached_connection() -> None:
    first_connection = _FakeImapConnection(search_ids=b"")
    second_connection = _FakeImapConnection(search_ids=b"")
    factory = _RecordingFactory(first_connection, second_connection)
    reader = ImapReplyReader(
        host="imap.example.com",
        port=993,
        username="carlos",
        password="sk-test-password",
        connection_factory=factory,
    )
    reader.list_message_ids("from:example-corp.com")

    reader.close()

    assert first_connection.logged_out is True
    # A call after close() reopens a fresh connection rather than reusing the logged-out one.
    reader.list_message_ids("from:example-corp.com")
    assert len(factory.calls) == 2


def test_close_is_a_noop_when_nothing_was_ever_connected() -> None:
    reader, _ = _reader(_FakeImapConnection())
    reader.close()  # must not raise


def test_close_suppresses_a_failure_logging_out_of_an_already_broken_connection() -> None:
    class _RaisingLogoutConnection(_FakeImapConnection):
        def logout(self):
            raise OSError("simulated: connection already gone")

    connection = _RaisingLogoutConnection(search_ids=b"")
    reader, _ = _reader(connection)
    reader.list_message_ids("from:example-corp.com")

    reader.close()  # must not raise despite logout() failing


def test_a_dropped_connection_is_transparently_reconnected_and_retried_once() -> None:
    """A transport-level error (a dropped/broken socket) on the cached connection must not fail
    the whole call outright -- it's exactly the "server closed an idle connection" case a fresh
    reconnect-and-retry recovers from."""

    class _DropsOnceConnection(_FakeImapConnection):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.uid_calls = 0

        def uid(self, command: str, *args: str):
            self.uid_calls += 1
            if self.uid_calls == 1:
                raise imaplib.IMAP4.abort("simulated: connection dropped")
            return super().uid(command, *args)

    first_connection = _DropsOnceConnection(search_ids=b"1 2")
    second_connection = _FakeImapConnection(search_ids=b"1 2")
    factory = _RecordingFactory(first_connection, second_connection)
    reader = ImapReplyReader(
        host="imap.example.com",
        port=993,
        username="carlos",
        password="sk-test-password",
        connection_factory=factory,
    )

    result = reader.list_message_ids("from:example-corp.com")

    assert result == ["1", "2"]
    assert len(factory.calls) == 2  # the original connection, then one reconnect
    assert first_connection.logged_out is True  # the broken connection was closed, not reused


def test_a_non_connection_error_propagates_without_retrying() -> None:
    """A generic error (a real protocol/logic bug, not a dropped socket) must propagate as-is,
    not be silently retried and masked as if it were a transient connection failure."""
    connection = _FakeImapConnection(raise_on_uid=RuntimeError("simulated logic error"))
    reader, factory = _reader(connection)

    with pytest.raises(RuntimeError, match="simulated logic error"):
        reader.get_message("1")

    assert len(factory.calls) == 1  # no reconnect attempt


def test_del_closes_the_connection_defensively() -> None:
    connection = _FakeImapConnection(search_ids=b"")
    reader, _ = _reader(connection)
    reader.list_message_ids("from:example-corp.com")

    reader.__del__()

    assert connection.logged_out is True


def test_no_from_prefix_falls_back_to_the_whole_query() -> None:
    """A query without the `"from:"` prefix (defensive -- `monitor.py` always adds it in practice)
    is used as-is rather than raising; the IMAP search criterion is only ever a best-effort
    narrowing, never the real gate (see `monitor.InboxReplyMonitor`'s authoritative domain
    check)."""
    connection = _FakeImapConnection(search_ids=b"")
    reader, _ = _reader(connection)

    reader.list_message_ids("example-corp.com")

    assert connection.last_search_criteria == (None, "FROM", '"example-corp.com"')


# --------------------------------------------------------------------------------------------- #
# _run_with_hard_timeout() / SubprocessImapReplyReader (issue #102 follow-up, real report): the
# only mechanism that recovers from a truly stuck imaplib call is killing the OS process it runs
# in -- a thread-based timeout cannot, if the blocking call never releases the GIL. These target
# functions are deliberately plain, top-level, and picklable (required by multiprocessing's
# "spawn" start method), mirroring exactly the shape `_fetch_candidates()` itself has.
# --------------------------------------------------------------------------------------------- #


def _quick_return(value: str) -> str:
    return value


def _hang_forever() -> None:
    while True:
        time.sleep(1)


def _raise_value_error(message: str) -> None:
    raise ValueError(message)


def test_run_with_hard_timeout_returns_the_function_result() -> None:
    from rtbf_agent.plugins.followup.imap_reader import _run_with_hard_timeout

    result = _run_with_hard_timeout(_quick_return, ("hello",), hard_timeout=10.0)

    assert result == "hello"


def test_run_with_hard_timeout_propagates_an_exception_from_the_child() -> None:
    from rtbf_agent.plugins.followup.imap_reader import _run_with_hard_timeout

    with pytest.raises(ValueError, match="simulated failure"):
        _run_with_hard_timeout(_raise_value_error, ("simulated failure",), hard_timeout=10.0)


def test_run_with_hard_timeout_kills_a_genuinely_hung_child_and_raises_timeouterror() -> None:
    """The core guarantee this whole mechanism exists for: a target that never returns (a real,
    genuinely blocking infinite loop in a separate process -- not a cooperative `asyncio` construct
    that would have to volunteer for cancellation) must not hang the caller past `hard_timeout`."""
    from rtbf_agent.plugins.followup.imap_reader import _run_with_hard_timeout

    with pytest.raises(TimeoutError):
        _run_with_hard_timeout(_hang_forever, (), hard_timeout=0.3)


class _FakeAsyncResult:
    def __init__(self, *, value=None, exc=None, hangs=False):
        self._value = value
        self._exc = exc
        self._hangs = hangs

    def get(self, timeout=None):
        if self._hangs:
            import multiprocessing as mp

            raise mp.TimeoutError()
        if self._exc is not None:
            raise self._exc
        return self._value


class _FakePool:
    """Stands in for `multiprocessing.Pool` -- proves `SubprocessImapReplyReader`'s wiring without
    ever spawning a real process, mirroring how `_FakeImapConnection` stands in for `imaplib`."""

    def __init__(self, *, value=None, exc=None, hangs=False):
        self._async_result = _FakeAsyncResult(value=value, exc=exc, hangs=hangs)
        self.terminated = False
        self.joined = False

    def apply_async(self, func, args):
        return self._async_result

    def terminate(self):
        self.terminated = True

    def join(self):
        self.joined = True


class _FakeMpContext:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    def Pool(self, processes: int):
        return self._pool


def test_subprocess_imap_reply_reader_caches_fetched_messages_for_get_message() -> None:
    from rtbf_agent.plugins.followup.imap_reader import SubprocessImapReplyReader

    reply = MailboxMessage(
        message_id="1",
        from_address="Support <support@example-corp.com>",
        subject="Re: deletion request",
        body="We have deleted your account.",
        received_at=datetime(2026, 7, 10, 14, 30, tzinfo=UTC),
    )
    pool = _FakePool(value=[reply])
    reader = SubprocessImapReplyReader(
        host="imap.example.com",
        port=993,
        username="carlos",
        password="sk-test-password",
        mp_context=_FakeMpContext(pool),
    )

    ids = reader.list_message_ids("from:example-corp.com")

    assert list(ids) == ["1"]
    assert reader.get_message("1") == reply
    assert pool.terminated is True  # the pool is always torn down, success or failure


def test_subprocess_imap_reply_reader_raises_timeouterror_when_the_child_hangs() -> None:
    from rtbf_agent.plugins.followup.imap_reader import SubprocessImapReplyReader

    pool = _FakePool(hangs=True)
    reader = SubprocessImapReplyReader(
        host="imap.example.com",
        port=993,
        username="carlos",
        password="sk-test-password",
        hard_timeout=0.01,
        mp_context=_FakeMpContext(pool),
    )

    with pytest.raises(TimeoutError):
        reader.list_message_ids("from:example-corp.com")

    assert pool.terminated is True


def test_subprocess_imap_reply_reader_satisfies_mailbox_reader_protocol() -> None:
    from rtbf_agent.plugins.followup.imap_reader import SubprocessImapReplyReader

    reader = SubprocessImapReplyReader(
        host="imap.example.com", port=993, username="carlos", password="sk-test-password"
    )
    assert isinstance(reader, MailboxReader)
