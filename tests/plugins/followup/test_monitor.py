"""Tests for `InboxReplyMonitor` (issue #68): the reply-matching heuristic.

The fake `MailboxReader` deliberately *ignores* the `from:<domain>` query and returns every
message, so these tests prove the Python-side matching (sender domain, arrived-after-send,
not-already-processed) is the authoritative gate -- a loose or ignored query can never let the
wrong message through, and a true reply is still found. Covers both a true positive and a plausible
false positive, as issue #68 requires.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from rtbf_agent.core.models import Case
from rtbf_agent.plugins.followup import monitor as monitor_module
from rtbf_agent.plugins.followup.monitor import InboxReplyMonitor, MailboxMessage

_SENT_AT = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


class _FakeMailboxReader:
    """Returns injected messages, ignoring the query -- proving Python-side matching is the gate."""

    def __init__(self, messages: list[MailboxMessage]) -> None:
        self._messages = {message.message_id: message for message in messages}
        self.queries: list[str] = []

    def list_message_ids(self, query: str) -> Sequence[str]:
        self.queries.append(query)
        return list(self._messages)

    def get_message(self, message_id: str) -> MailboxMessage:
        return self._messages[message_id]


def _case() -> Case:
    return Case(
        id=1,
        site="Example Corp",
        domain="example.com",
        target_email="user@gmail.com",
        recipient_email="privacy@example-corp.com",
    )


def _message(
    message_id: str,
    *,
    from_address: str,
    minutes_after_send: int,
    body: str = "reply body",
    subject: str = "Re: deletion request",
) -> MailboxMessage:
    return MailboxMessage(
        message_id=message_id,
        from_address=from_address,
        subject=subject,
        body=body,
        received_at=_SENT_AT + timedelta(minutes=minutes_after_send),
    )


async def _find(messages: list[MailboxMessage], *, processed: frozenset[str] = frozenset()):
    monitor = InboxReplyMonitor(_FakeMailboxReader(messages))
    return await monitor.find_new_reply(_case(), sent_at=_SENT_AT, already_processed_ids=processed)


async def test_true_positive_reply_from_recipient_domain_after_send_is_found() -> None:
    reply = _message("m1", from_address="Support <support@example-corp.com>", minutes_after_send=60)
    found = await _find([reply])
    assert found is not None
    assert found.message_id == "m1"


async def test_reply_from_a_subdomain_of_the_recipient_domain_is_matched() -> None:
    reply = _message("m1", from_address="noreply@tickets.example-corp.com", minutes_after_send=30)
    found = await _find([reply])
    assert found is not None


async def test_false_positive_message_from_a_different_domain_is_not_matched() -> None:
    """A newsletter from an unrelated domain, arriving after the send, must never be attributed to
    this case -- too loose a heuristic would misroute another sender's mail into this case."""
    unrelated = _message("m1", from_address="news@other-company.com", minutes_after_send=60)
    assert await _find([unrelated]) is None


async def test_lookalike_domain_is_not_mistaken_for_the_recipient_domain() -> None:
    lookalike = _message("m1", from_address="x@notexample-corp.com", minutes_after_send=60)
    evil = _message("m2", from_address="x@example-corp.com.evil.com", minutes_after_send=60)
    assert await _find([lookalike, evil]) is None


async def test_message_arriving_before_the_send_is_not_a_reply() -> None:
    """The original signup/confirmation mail from the same domain predates the send and must be
    excluded, or it would be misread as a reply."""
    pre_send = _message("m1", from_address="welcome@example-corp.com", minutes_after_send=-120)
    assert await _find([pre_send]) is None


async def test_message_at_exactly_the_send_time_is_excluded() -> None:
    at_send = _message("m1", from_address="x@example-corp.com", minutes_after_send=0)
    assert await _find([at_send]) is None


async def test_already_processed_message_is_skipped() -> None:
    reply = _message("m1", from_address="support@example-corp.com", minutes_after_send=60)
    assert await _find([reply], processed=frozenset({"m1"})) is None


async def test_oldest_unprocessed_matching_reply_is_returned_first() -> None:
    newer = _message("newer", from_address="x@example-corp.com", minutes_after_send=120)
    older = _message("older", from_address="x@example-corp.com", minutes_after_send=30)
    found = await _find([newer, older])
    assert found is not None
    assert found.message_id == "older"


async def test_returns_none_when_no_messages_at_all() -> None:
    assert await _find([]) is None


async def test_falls_back_to_case_domain_when_no_recipient_email() -> None:
    case = _case()
    case.recipient_email = None  # not normally reachable for a sent case; defensive fallback
    monitor = InboxReplyMonitor(
        _FakeMailboxReader([_message("m1", from_address="x@example.com", minutes_after_send=60)])
    )
    found = await monitor.find_new_reply(case, sent_at=_SENT_AT, already_processed_ids=frozenset())
    assert found is not None


async def test_query_is_scoped_to_the_expected_domain() -> None:
    reader = _FakeMailboxReader([])
    monitor = InboxReplyMonitor(reader)
    await monitor.find_new_reply(_case(), sent_at=_SENT_AT, already_processed_ids=frozenset())
    assert reader.queries == ["from:example-corp.com"]


async def test_query_is_unaffected_by_whether_a_sent_message_id_is_given() -> None:
    """Real report: Gmail's IMAP `SEARCH HEADER` can't find a reply by `In-Reply-To`/`References`
    server-side at all (see `ImapReplyReader`'s module docstring) -- `in_reply_to_message_id` is
    used for client-side candidate selection only (see `find_new_reply()`'s docstring), never to
    narrow the mailbox query itself, so the query stays exactly `from:<domain>` either way."""
    reader = _FakeMailboxReader([])
    monitor = InboxReplyMonitor(reader)
    await monitor.find_new_reply(
        _case(),
        sent_at=_SENT_AT,
        already_processed_ids=frozenset(),
        in_reply_to_message_id="<abc123@example.com>",
    )
    assert reader.queries == ["from:example-corp.com"]


async def test_a_candidate_whose_headers_reference_the_sent_message_id_is_preferred() -> None:
    """The actual fix for the over-matching real report: when `Case.recipient_email` is wrong (even
    the user's own address), *every* self-received message satisfies the domain check -- but only
    one, if any, genuinely references what we sent. That one must win over an unrelated message that
    merely arrived later."""
    unrelated_but_newer = _message(
        "unrelated", from_address="x@example-corp.com", minutes_after_send=120
    )
    the_actual_reply = MailboxMessage(
        message_id="reply",
        from_address="x@example-corp.com",
        subject="Re: deletion request",
        body="reply body",
        received_at=_SENT_AT + timedelta(minutes=60),
        in_reply_to_ids=frozenset({"<sent@example.com>"}),
    )
    reader = _FakeMailboxReader([unrelated_but_newer, the_actual_reply])
    monitor = InboxReplyMonitor(reader)

    found = await monitor.find_new_reply(
        _case(),
        sent_at=_SENT_AT,
        already_processed_ids=frozenset(),
        in_reply_to_message_id="<sent@example.com>",
    )

    assert found is not None
    assert found.message_id == "reply"


async def test_falls_back_to_oldest_when_no_candidate_references_the_sent_message_id() -> None:
    """A case sent before this feature existed (or a candidate whose mail client didn't set
    References/In-Reply-To) has nothing to prefer by -- the plain oldest-candidate rule still
    applies, exactly as before this preference existed."""
    older = _message("older", from_address="x@example-corp.com", minutes_after_send=30)
    newer = _message("newer", from_address="x@example-corp.com", minutes_after_send=60)
    reader = _FakeMailboxReader([newer, older])
    monitor = InboxReplyMonitor(reader)

    found = await monitor.find_new_reply(
        _case(),
        sent_at=_SENT_AT,
        already_processed_ids=frozenset(),
        in_reply_to_message_id="<sent@example.com>",
    )

    assert found is not None
    assert found.message_id == "older"


async def test_recipient_email_with_a_display_name_prefix_still_matches_correctly() -> None:
    """`case_actions.confirm_recipient()` only validates that the typed value contains "@", not that
    it's a bare address with no display name -- a value like "Privacy Team <privacy@company.com>"
    can genuinely reach `Case.recipient_email`. The expected domain must still resolve to
    "example-corp.com", not "example-corp.com>" (the raw-`rsplit` bug this test guards against)."""
    case = _case()
    case.recipient_email = "Privacy Team <privacy@example-corp.com>"
    reply = _message("m1", from_address="Support <support@example-corp.com>", minutes_after_send=60)

    found = await InboxReplyMonitor(_FakeMailboxReader([reply])).find_new_reply(
        case, sent_at=_SENT_AT, already_processed_ids=frozenset()
    )

    assert found is not None
    assert found.message_id == "m1"


class _HangingMailboxReader:
    """A `MailboxReader` whose `get_message()` blocks (genuinely, via `time.sleep` -- not an
    `asyncio` primitive, mirroring a real blocking `imaplib`/network call run through
    `asyncio.to_thread()`) far longer than the test's patched timeout."""

    def __init__(self, message_ids: list[str]) -> None:
        self._message_ids = message_ids

    def list_message_ids(self, query: str) -> Sequence[str]:
        return self._message_ids

    def get_message(self, message_id: str) -> MailboxMessage:
        # 20x the patched timeout below -- long enough to prove `wait_for()` gives up well before
        # this would ever return, short enough that the orphaned background thread (which keeps
        # running after the `await` times out -- Python threads cannot be forcibly killed, see the
        # module-level `_READER_CALL_TIMEOUT_SECONDS` docstring) doesn't meaningfully slow down the
        # test suite while the event loop joins it at shutdown.
        time.sleep(1)
        raise AssertionError("must never actually complete -- the timeout must fire first")


async def test_a_reader_call_that_hangs_forever_times_out_instead_of_blocking_the_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real report: a genuine `MailboxReader` implementation (`ImapReplyReader`, over a real
    account) hung completely inside a single blocking call, for minutes, even though that reader
    has its own internal timeout -- a reader-level timeout cannot be fully trusted to always fire.
    This is the `asyncio`-layer backstop: `find_new_reply()` must raise `TimeoutError` rather than
    hang forever, regardless of what the underlying blocking call is actually doing (proven here
    with genuine blocking `time.sleep`, not a cooperative `asyncio` cancellation the hung call
    would have to volunteer for -- a truly stuck real call never would)."""
    monkeypatch.setattr(monitor_module, "_READER_CALL_TIMEOUT_SECONDS", 0.05)
    monitor = InboxReplyMonitor(_HangingMailboxReader(["m1"]))

    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await monitor.find_new_reply(_case(), sent_at=_SENT_AT, already_processed_ids=frozenset())
