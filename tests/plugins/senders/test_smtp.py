"""Tests for `rtbf_agent.plugins.senders.smtp` — the first concrete `Sender` (issue #35).

Never opens a real network/SMTP connection: `SMTPSender` takes an injected
`smtp_client_factory`, and every test here passes a fake, in-memory class shaped like
`smtplib.SMTP` instead (mirrors `tests/plugins/llm/test_network_isolation.py`'s no-real-network
discipline, applied here via dependency injection rather than a local server since `smtplib` has
no httpx-style transport seam).
"""

from __future__ import annotations

from email.message import EmailMessage as MimeEmailMessage

import pytest

from rtbf_agent.core.interfaces import DocumentAttachment, EmailMessage, Sender
from rtbf_agent.plugins.senders.config import SMTPConfig
from rtbf_agent.plugins.senders.smtp import SMTPSender


class _FakeSMTPClient:
    """An in-memory double for `smtplib.SMTP`, recording every call made to it."""

    instances: list[_FakeSMTPClient] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_calls: list[tuple[str, str]] = []
        self.sent_messages: list[MimeEmailMessage] = []
        self.entered = False
        self.exited = False
        type(self).instances.append(self)

    def __enter__(self) -> _FakeSMTPClient:
        self.entered = True
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.exited = True

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, username: str, password: str) -> None:
        self.login_calls.append((username, password))

    def send_message(self, msg: MimeEmailMessage) -> None:
        self.sent_messages.append(msg)


class _RaisingSMTPClient(_FakeSMTPClient):
    """Raises on `send_message` -- simulates a transport-level failure."""

    def send_message(self, msg: MimeEmailMessage) -> None:
        raise OSError("connection refused")


@pytest.fixture(autouse=True)
def _reset_fake_instances():
    _FakeSMTPClient.instances = []
    yield
    _FakeSMTPClient.instances = []


def _config(**overrides) -> SMTPConfig:
    defaults = dict(
        host="smtp.example.com",
        port=587,
        username="carlos",
        password="super-secret-password",
        from_address="carlos@example.com",
        use_tls=True,
    )
    defaults.update(overrides)
    return SMTPConfig(**defaults)


def test_smtpsender_implements_sender_contract() -> None:
    """`SMTPSender` is a concrete `Sender` -- the ABC's abstract `send` method is implemented."""
    sender = SMTPSender(_config(), smtp_client_factory=_FakeSMTPClient)
    assert isinstance(sender, Sender)


async def test_send_dispatches_over_the_injected_smtp_client() -> None:
    sender = SMTPSender(_config(), smtp_client_factory=_FakeSMTPClient)
    message = EmailMessage(
        case_id="42",
        to_address="controller@example.com",
        subject="Deletion request",
        body="Please delete my data.",
    )

    await sender.send(message)

    assert len(_FakeSMTPClient.instances) == 1
    client = _FakeSMTPClient.instances[0]
    assert client.entered and client.exited
    assert client.started_tls
    assert client.login_calls == [("carlos", "super-secret-password")]
    assert len(client.sent_messages) == 1
    mime_message = client.sent_messages[0]
    assert mime_message["To"] == "controller@example.com"
    assert mime_message["Subject"] == "Deletion request"
    assert mime_message["From"] == "carlos@example.com"
    assert mime_message.get_content().strip() == "Please delete my data."


async def test_send_sets_the_message_id_header_when_provided() -> None:
    sender = SMTPSender(_config(), smtp_client_factory=_FakeSMTPClient)
    message = EmailMessage(
        case_id="42",
        to_address="controller@example.com",
        subject="Deletion request",
        body="Please delete my data.",
        message_id="<abc123@example.com>",
    )

    await sender.send(message)

    mime_message = _FakeSMTPClient.instances[0].sent_messages[0]
    assert mime_message["Message-ID"] == "<abc123@example.com>"


async def test_send_leaves_message_id_unset_when_not_provided() -> None:
    sender = SMTPSender(_config(), smtp_client_factory=_FakeSMTPClient)
    message = EmailMessage(
        case_id="42",
        to_address="controller@example.com",
        subject="Deletion request",
        body="Please delete my data.",
    )

    await sender.send(message)

    mime_message = _FakeSMTPClient.instances[0].sent_messages[0]
    assert mime_message["Message-ID"] is None


async def test_send_sets_in_reply_to_and_references_when_provided() -> None:
    sender = SMTPSender(_config(), smtp_client_factory=_FakeSMTPClient)
    message = EmailMessage(
        case_id="42",
        to_address="controller@example.com",
        subject="Re: Deletion request",
        body="In response to your request for additional information: ...",
        in_reply_to="<reply@example.com>",
        references="<sent@example.com> <reply@example.com>",
    )

    await sender.send(message)

    mime_message = _FakeSMTPClient.instances[0].sent_messages[0]
    assert mime_message["In-Reply-To"] == "<reply@example.com>"
    assert mime_message["References"] == "<sent@example.com> <reply@example.com>"


async def test_send_leaves_in_reply_to_and_references_unset_when_not_provided() -> None:
    sender = SMTPSender(_config(), smtp_client_factory=_FakeSMTPClient)
    message = EmailMessage(
        case_id="42",
        to_address="controller@example.com",
        subject="Deletion request",
        body="Please delete my data.",
    )

    await sender.send(message)

    mime_message = _FakeSMTPClient.instances[0].sent_messages[0]
    assert mime_message["In-Reply-To"] is None
    assert mime_message["References"] is None


async def test_send_attaches_document_when_present() -> None:
    sender = SMTPSender(_config(), smtp_client_factory=_FakeSMTPClient)
    message = EmailMessage(
        case_id="42",
        to_address="controller@example.com",
        subject="Deletion request follow-up",
        body="Please find the requested identity document attached.",
        attachments=(
            DocumentAttachment(
                filename="id-scan.png", mime_type="image/png", content=b"fake image bytes"
            ),
        ),
    )

    await sender.send(message)

    mime_message = _FakeSMTPClient.instances[0].sent_messages[0]
    attachments = list(mime_message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "id-scan.png"
    assert attachments[0].get_content_type() == "image/png"
    assert attachments[0].get_content() == b"fake image bytes"


async def test_send_attaches_multiple_documents_when_present() -> None:
    """Issue #111 (document vault): more than one document may be selected for a single resend."""
    sender = SMTPSender(_config(), smtp_client_factory=_FakeSMTPClient)
    message = EmailMessage(
        case_id="42",
        to_address="controller@example.com",
        subject="Deletion request follow-up",
        body="Please find the requested documents attached.",
        attachments=(
            DocumentAttachment(filename="id-scan.png", mime_type="image/png", content=b"id"),
            DocumentAttachment(filename="proof.pdf", mime_type="application/pdf", content=b"proof"),
        ),
    )

    await sender.send(message)

    mime_message = _FakeSMTPClient.instances[0].sent_messages[0]
    attachments = list(mime_message.iter_attachments())
    assert len(attachments) == 2
    assert {attachment.get_filename() for attachment in attachments} == {
        "id-scan.png",
        "proof.pdf",
    }


async def test_send_has_no_attachment_part_when_none_provided() -> None:
    sender = SMTPSender(_config(), smtp_client_factory=_FakeSMTPClient)
    message = EmailMessage(case_id="42", to_address="controller@example.com", subject="s", body="b")

    await sender.send(message)

    mime_message = _FakeSMTPClient.instances[0].sent_messages[0]
    assert list(mime_message.iter_attachments()) == []


async def test_send_skips_starttls_when_use_tls_is_false() -> None:
    sender = SMTPSender(_config(use_tls=False), smtp_client_factory=_FakeSMTPClient)
    message = EmailMessage(case_id="1", to_address="x@example.com", subject="s", body="b")

    await sender.send(message)

    assert not _FakeSMTPClient.instances[0].started_tls


async def test_send_skips_login_when_no_username_configured() -> None:
    sender = SMTPSender(_config(username=None, password=None), smtp_client_factory=_FakeSMTPClient)
    message = EmailMessage(case_id="1", to_address="x@example.com", subject="s", body="b")

    await sender.send(message)

    assert _FakeSMTPClient.instances[0].login_calls == []


async def test_send_propagates_transport_failures_without_swallowing_them() -> None:
    """`send()` must not catch and suppress a transport-level failure (R1): the caller
    (`agent_loop._execute_send()`) depends on the exception propagating."""
    sender = SMTPSender(_config(), smtp_client_factory=_RaisingSMTPClient)
    message = EmailMessage(case_id="1", to_address="x@example.com", subject="s", body="b")

    with pytest.raises(OSError, match="connection refused"):
        await sender.send(message)


async def test_send_never_puts_the_password_in_the_outgoing_message_or_its_repr() -> None:
    """Credentials must never leak into anything that could end up logged or audited (R14):
    the constructed MIME message and `SMTPConfig`'s own `repr()` must never surface the password.
    """
    config = _config(password="totally-secret-value")
    sender = SMTPSender(config, smtp_client_factory=_FakeSMTPClient)
    message = EmailMessage(case_id="1", to_address="x@example.com", subject="s", body="b")

    await sender.send(message)

    client = _FakeSMTPClient.instances[0]
    mime_message = client.sent_messages[0]
    assert "totally-secret-value" not in str(mime_message)
    assert "totally-secret-value" not in repr(config)
