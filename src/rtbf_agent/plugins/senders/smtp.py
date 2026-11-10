"""`SMTPSender` — the first concrete `Sender` (R1, R12, R13, R14, R21).

Uses stdlib `smtplib` directly (no third-party dependency) against the user's own SMTP account --
never a project-operated relay (R12/R13). Credentials are resolved via `plugins/senders/config.py`
(env-var-then-local-config-file, mirroring `plugins/llm/config.py`) and are never logged or
returned from any method here; nothing in this module writes to an `Event` or any other audit
trail -- that stays `agent_loop._execute_send()`'s job, entirely after this class's `send()` call
succeeds (R1).

`smtplib.SMTP` performs blocking socket I/O with no async API of its own, so `send()` (async, per
the `Sender` contract) runs the blocking call in a worker thread via `asyncio.to_thread` rather than
blocking the event loop.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage as MimeEmailMessage
from typing import Protocol

from rtbf_agent.core.interfaces import EmailMessage, Sender
from rtbf_agent.plugins.senders.config import SMTPConfig, resolve_smtp_config

DEFAULT_TIMEOUT_SECONDS = 30.0


class SMTPClientProtocol(Protocol):
    """The subset of `smtplib.SMTP`'s interface this module depends on.

    Exists so tests can inject a fake, in-memory double shaped like `smtplib.SMTP` instead of
    constructing a real one -- `SMTPSender` never opens a real network socket in a test (mirrors
    `tests/plugins/llm/test_network_isolation.py`'s no-real-network-in-tests discipline, applied
    here via dependency injection rather than a local server, since `smtplib` has no httpx-style
    transport seam to swap).
    """

    def __enter__(self) -> SMTPClientProtocol: ...

    def __exit__(self, *exc_info: object) -> None: ...

    def starttls(self) -> None: ...

    def login(self, username: str, password: str) -> None: ...

    def send_message(self, msg: MimeEmailMessage) -> None: ...


class SMTPSender(Sender):
    """Sends one email per `send()` call over SMTP. Stateless across calls beyond its config."""

    def __init__(
        self,
        config: SMTPConfig | None = None,
        *,
        smtp_client_factory: type[SMTPClientProtocol] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """`config` defaults to `resolve_smtp_config()` (env vars, then local config file -- R14).

        `smtp_client_factory` is the injection point tests use to avoid a real SMTP connection: a
        fake class shaped like `SMTPClientProtocol`, constructed the same way `smtplib.SMTP` is
        (`factory(host, port, timeout=timeout)`). Production callers omit it; it defaults to
        `smtplib.SMTP`.
        """
        self._config = config if config is not None else resolve_smtp_config()
        self._smtp_client_factory = smtp_client_factory or smtplib.SMTP
        self._timeout = timeout

    async def send(self, message: EmailMessage) -> None:
        """Dispatch `message` over SMTP, running the blocking `smtplib` call off the event loop.

        Raises whatever `smtplib`/`asyncio.to_thread` raises (e.g. `smtplib.SMTPException`,
        `OSError`) rather than catching and suppressing it -- `agent_loop._execute_send()` depends
        on that propagating so it never records a `request_sent` `Event` or transitions
        `Case.state` for a message that was never actually delivered (R1).
        """
        await asyncio.to_thread(self._send_sync, message)

    def _send_sync(self, message: EmailMessage) -> None:
        mime_message = MimeEmailMessage()
        mime_message["From"] = self._config.from_address
        mime_message["To"] = message.to_address
        mime_message["Subject"] = message.subject
        mime_message.set_content(message.body)
        if message.message_id:
            # See `EmailMessage.message_id`'s docstring: lets a later follow-up pass search by
            # `In-Reply-To`/`References` instead of a broad sender-domain guess. Left to
            # `smtplib`/the outbound MTA, as before, when the caller doesn't provide one.
            mime_message["Message-ID"] = message.message_id
        if message.in_reply_to:
            # See `EmailMessage.in_reply_to`'s docstring: without this, a resend after an
            # additional-info round looked like a fresh, unrelated email in the recipient's mail
            # client instead of landing in the existing conversation.
            mime_message["In-Reply-To"] = message.in_reply_to
        if message.references:
            mime_message["References"] = message.references

        # issue #69/#111: a followup resend that includes one or more requested identity documents
        # (a document vault lets more than one be selected at once). `content` is only ever in
        # memory here -- never written to disk or logged by this module.
        for attachment in message.attachments:
            maintype, _, subtype = attachment.mime_type.partition("/")
            mime_message.add_attachment(
                attachment.content,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=attachment.filename,
            )

        with self._smtp_client_factory(
            self._config.host, self._config.port, timeout=self._timeout
        ) as client:
            if self._config.use_tls:
                client.starttls()
            if self._config.username:
                # `resolve_smtp_config()` already guarantees a `username` never appears without a
                # `password` (raises `LookupError` at resolution time otherwise), so this is never
                # reached with `password is None`.
                assert self._config.password is not None
                client.login(self._config.username, self._config.password)
            client.send_message(mime_message)
