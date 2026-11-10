"""`TelegramNotifier` -- the second real `Notifier` (after `ConsoleNotifier`, issue #9), backed by
`python-telegram-bot`.

**Notify-only (automated-case-loop rework):** this used to also solicit approvals (an
`InlineKeyboardMarkup` a human pressed) and receive documents/text replies, each via a live,
blocking wait requiring the bot's `Application` to be polling for updates the whole time. All of
that is gone -- see `core.interfaces.Notifier`'s module docstring for why: every decision is now
recorded through `case_actions.py` (kanban route or CLI subcommand), never by this notifier
blocking on an incoming Telegram update. `notify()` alone needs no polling loop at all -- it is a
single outbound `sendMessage` call, nothing more.

A later feature (explicitly deferred, not built here) is inline-keyboard buttons that call
`case_actions` functions directly from a callback-query handler -- genuinely new scaffolding when
it lands, not a restoration of what this module used to do, since the old design resolved an
in-process `asyncio.Future` a single invocation was waiting on, which doesn't fit a notifier that
no longer blocks on anything.

Enforces:
- **R15** -- the whitelisted `chat_id` is fixed at construction time (one bot, one operator, per
  spec's "one bot per user, self-hosted" model).
- **R23** -- the one user-facing string this module sends (the notification text) is resolved via
  `I18nProvider.translate()`; no literal string is ever passed to `TelegramBotClient.send_message`
  (see `tests/plugins/notifiers/test_telegram.py::
  test_no_literal_user_facing_strings_in_bot_code`).

`python-telegram-bot` itself is imported lazily, only inside `PythonTelegramBotClient` -- the one
place that needs the real library to talk to Telegram -- mirroring `plugins/discovery/gmail.py`'s
lazy import of `googleapiclient`/`google_auth_oauthlib`. `TelegramNotifier` itself depends only on
the narrow `TelegramBotClient` Protocol below, so it is fully testable against an injected fake bot
client -- no real Telegram API call, and no network access, in any test.
"""

from __future__ import annotations

from typing import Any, Protocol

from rtbf_agent.core.interfaces import I18nProvider, NotificationMessage, Notifier


class TelegramBotClient(Protocol):
    """The narrow outbound surface `TelegramNotifier` depends on -- the test/DI seam.

    Shaped after (but not implemented in terms of) `telegram.Bot`'s async API, mirroring
    `plugins/senders/smtp.SMTPClientProtocol` and `plugins/discovery/gmail.GmailClient`: any object
    providing this method satisfies this Protocol, so tests inject a fake recording calls instead
    of a real `telegram.Bot` making real HTTP requests to Telegram's servers.
    """

    async def send_message(self, chat_id: str, text: str) -> Any: ...


class PythonTelegramBotClient:
    """`TelegramBotClient` backed by a real `telegram.Bot` -- the only production implementation.

    The only place in this module that imports `python-telegram-bot`, and it does so lazily inside
    `__init__` so the rest of this module -- and every test of `TelegramNotifier` itself -- never
    requires the `telegram` optional dependency group installed (mirrors `plugins/discovery/
    gmail.py`'s `build_gmail_client`/`build_installed_app_flow`).
    """

    def __init__(self, token: str) -> None:
        from telegram import Bot

        self._bot = Bot(token=token)

    async def send_message(self, chat_id: str, text: str) -> Any:
        return await self._bot.send_message(chat_id=chat_id, text=text)


class TelegramNotifier(Notifier):
    """`Notifier` implementation sending one-way notices to one whitelisted Telegram `chat_id`
    (R15, R23). See module docstring for why this is notify-only."""

    def __init__(
        self,
        *,
        bot: TelegramBotClient,
        chat_id: str,
        i18n: I18nProvider,
        lang: str,
    ) -> None:
        self._bot = bot
        self._chat_id = str(chat_id)
        self._i18n = i18n
        self._lang = lang

    async def notify(self, message: NotificationMessage) -> None:
        """Send a one-way notice. Never solicits or returns a decision (R1)."""
        text = self._i18n.translate(
            "telegram.notification", self._lang, case_id=message.case_id, text=message.text
        )
        await self._bot.send_message(self._chat_id, text)
