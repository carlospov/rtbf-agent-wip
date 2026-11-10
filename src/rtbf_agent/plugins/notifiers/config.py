"""Local-only config resolution for `TelegramNotifier` (R14, R15).

Env-var-only -- deliberately not `RTBF_`-prefixed, matching the placeholder names already
reserved in `.env.example` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) since before this module
existed (issue #14 is what makes them real). Unlike `plugins/senders/config.py` or
`plugins/llm/config.py`, there is deliberately no local-config-file fallback here: this repo is
"one bot per user, self-hosted" (spec), so the whitelisted `chat_id` is a single value with no
legitimate multi-profile use case, and env-var-only keeps its provenance to exactly one place --
never silently reintroduced from a stale, hand-edited local config file a user forgot they still
had (R15 depends on the configured whitelist being exactly what the operator currently intends).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TelegramConfig:
    """A fully-resolved Telegram bot token + the single whitelisted `chat_id` (R14, R15).

    `bot_token` is `repr=False` so an accidental `repr()`/`str()` of this object (traceback,
    debugger, stray `print`/log call) never surfaces the secret -- mirrors
    `plugins/senders/config.SMTPConfig.password`.
    """

    bot_token: str = field(repr=False)
    chat_id: str


def resolve_telegram_config() -> TelegramConfig:
    """Resolve `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` from the environment -- nothing else.

    Raises `LookupError` naming exactly which of the two is missing/blank if either isn't set --
    never falls back to a placeholder token or an unrestricted (no-whitelist) mode, since a
    `TelegramNotifier` constructed without a real `chat_id` would have no whitelist to enforce at
    all (R15).
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    missing = [
        name
        for name, value in (("TELEGRAM_BOT_TOKEN", bot_token), ("TELEGRAM_CHAT_ID", chat_id))
        if not value
    ]
    if missing:
        raise LookupError(
            f"incomplete Telegram config: missing {', '.join(missing)} "
            "(set as environment variables -- see .env.example)"
        )

    return TelegramConfig(bot_token=bot_token, chat_id=chat_id)
