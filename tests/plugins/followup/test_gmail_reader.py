"""Tests for `GmailReplyReader` (issue #68): a read-only `MailboxReader` over the Gmail API.

Exercised against a fake `googleapiclient`-shaped resource -- never a real Gmail account or network
call, exactly as `tests/plugins/discovery/test_gmail.py` fakes the discovery client.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

from rtbf_agent.plugins.followup.gmail_reader import GmailReplyReader
from rtbf_agent.plugins.followup.monitor import MailboxReader


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


class _FakeMessages:
    def __init__(self, list_response: dict, messages: dict[str, dict]) -> None:
        self._list_response = list_response
        self._messages = messages

    def list(self, *, userId: str, q: str):  # noqa: N803 (Gmail API kwarg name)
        self._last_query = q
        return _FakeExecute(self._list_response)

    def get(self, *, userId: str, id: str, format: str):  # noqa: N803, A002
        return _FakeExecute(self._messages[id])


class _FakeUsers:
    def __init__(self, messages: _FakeMessages) -> None:
        self._messages = messages

    def messages(self) -> _FakeMessages:
        return self._messages


class _FakeResource:
    def __init__(self, list_response: dict, messages: dict[str, dict]) -> None:
        self._users = _FakeUsers(_FakeMessages(list_response, messages))

    def users(self) -> _FakeUsers:
        return self._users


class _FakeExecute:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def execute(self) -> dict:
        return self._payload


def _plain_message(
    *, body: str, date: str = "Fri, 10 Jul 2026 14:30:00 +0000", message_id: str | None = None
) -> dict:
    headers = [
        {"name": "From", "value": "Support <support@example-corp.com>"},
        {"name": "Subject", "value": "Re: deletion request"},
        {"name": "Date", "value": date},
    ]
    if message_id is not None:
        headers.append({"name": "Message-ID", "value": message_id})
    return {
        "payload": {
            "headers": headers,
            "mimeType": "text/plain",
            "body": {"data": _b64url(body)},
        }
    }


def test_gmail_reply_reader_satisfies_mailbox_reader_protocol() -> None:
    reader = GmailReplyReader(_FakeResource({"messages": []}, {}))
    assert isinstance(reader, MailboxReader)


def test_list_message_ids_returns_ids_and_scopes_the_query() -> None:
    resource = _FakeResource({"messages": [{"id": "a"}, {"id": "b"}]}, {})
    reader = GmailReplyReader(resource)
    assert list(reader.list_message_ids("from:example-corp.com")) == ["a", "b"]


def test_get_message_extracts_headers_body_and_date() -> None:
    resource = _FakeResource(
        {"messages": [{"id": "m1"}]},
        {"m1": _plain_message(body="We have deleted your account.")},
    )
    reader = GmailReplyReader(resource)
    message = reader.get_message("m1")
    assert message.message_id == "m1"
    assert message.from_address == "Support <support@example-corp.com>"
    assert message.subject == "Re: deletion request"
    assert message.body == "We have deleted your account."
    assert message.received_at == datetime(2026, 7, 10, 14, 30, tzinfo=UTC)
    assert message.rfc822_message_id is None


def test_get_message_parses_the_messages_own_message_id() -> None:
    """Real report: without this, a later resend threading back to this exact message
    (`agent_loop._thread_reference_ids()`) has nothing to put in `In-Reply-To`/`References`."""
    resource = _FakeResource(
        {"messages": [{"id": "m1"}]},
        {
            "m1": _plain_message(
                body="We have deleted your account.",
                message_id="<reply-from-site@example-corp.com>",
            )
        },
    )
    reader = GmailReplyReader(resource)
    assert reader.get_message("m1").rfc822_message_id == "<reply-from-site@example-corp.com>"


def test_get_message_prefers_text_plain_within_a_multipart_tree() -> None:
    multipart = {
        "payload": {
            "headers": [{"name": "Date", "value": "Fri, 10 Jul 2026 14:30:00 +0000"}],
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64url("<p>html version</p>")}},
                {"mimeType": "text/plain", "body": {"data": _b64url("plain version")}},
            ],
        }
    }
    reader = GmailReplyReader(_FakeResource({"messages": [{"id": "m1"}]}, {"m1": multipart}))
    assert reader.get_message("m1").body == "plain version"


def test_get_message_returns_empty_body_for_undecodable_base64_data() -> None:
    """A malformed body `data` value (one whose length can never be valid base64, even after
    re-padding -- `base64.urlsafe_b64decode` raises `binascii.Error` for it) must never crash the
    monitoring pass -- an undecodable body is no worse to `ResponseClassifier` than a message with
    no body at all."""
    malformed = {
        "payload": {
            "headers": [{"name": "Date", "value": "Fri, 10 Jul 2026 14:30:00 +0000"}],
            "mimeType": "text/plain",
            "body": {"data": "A"},
        }
    }
    reader = GmailReplyReader(_FakeResource({"messages": [{"id": "m1"}]}, {"m1": malformed}))
    assert reader.get_message("m1").body == ""


def test_get_message_falls_back_to_html_when_no_plain_part() -> None:
    html_only = {
        "payload": {
            "headers": [{"name": "Date", "value": "Fri, 10 Jul 2026 14:30:00 +0000"}],
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64url("<p>only html</p>")}},
            ],
        }
    }
    reader = GmailReplyReader(_FakeResource({"messages": [{"id": "m1"}]}, {"m1": html_only}))
    assert "only html" in reader.get_message("m1").body


def test_get_message_with_unparseable_date_sorts_before_any_send_time() -> None:
    resource = _FakeResource(
        {"messages": [{"id": "m1"}]},
        {"m1": _plain_message(body="hi", date="not a date")},
    )
    message = GmailReplyReader(resource).get_message("m1")
    # Epoch -> strictly before any realistic send time, so a dateless message can never masquerade
    # as a fresh post-send reply.
    assert message.received_at < datetime(2000, 1, 1, tzinfo=UTC)


def test_get_message_assumes_utc_for_a_date_without_an_offset() -> None:
    resource = _FakeResource(
        {"messages": [{"id": "m1"}]},
        {"m1": _plain_message(body="hi", date="Fri, 10 Jul 2026 14:30:00")},
    )
    message = GmailReplyReader(resource).get_message("m1")
    assert message.received_at.tzinfo is not None
    assert message.received_at == datetime(2026, 7, 10, 14, 30, tzinfo=UTC)
