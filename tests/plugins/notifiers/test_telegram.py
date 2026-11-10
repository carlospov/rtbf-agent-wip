"""Tests for `rtbf_agent.plugins.notifiers.telegram.TelegramNotifier` (issue #14).

Notify-only after the automated-case-loop rework (see `TelegramNotifier`'s module docstring for
why the old approval/document/text-response solicitation, and the `Application`-polling machinery
it needed, were removed). No real Telegram API call and no network access anywhere here:
`TelegramNotifier` itself is exercised only against a fake `TelegramBotClient`;
`PythonTelegramBotClient` (the one place that touches the real `python-telegram-bot` library) is
exercised by swapping in a fake `telegram.Bot` after construction, never by letting a real HTTP
request happen.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from rtbf_agent.core.interfaces import I18nProvider, NotificationMessage, Notifier
from rtbf_agent.plugins.notifiers.telegram import PythonTelegramBotClient, TelegramNotifier

TELEGRAM_MODULE_PATH = Path(__file__).resolve().parents[3] / (
    "src/rtbf_agent/plugins/notifiers/telegram.py"
)
WHITELISTED_CHAT_ID = "111222333"


class _FakeI18n(I18nProvider):
    """Records every `translate()` call and returns deterministic, inspectable text."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def translate(self, key: str, lang: str, **params: object) -> str:
        self.calls.append((key, lang, params))
        if params:
            joined = ", ".join(f"{k}={v}" for k, v in sorted(params.items()))
            return f"[{lang}] {key} ({joined})"
        return f"[{lang}] {key}"


class _FakeBotClient:
    """Records every outbound call `TelegramNotifier` makes -- no real Telegram API involved."""

    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []

    async def send_message(self, chat_id, text):
        self.sent_messages.append({"chat_id": chat_id, "text": text})
        return None


def _make_notifier() -> tuple[TelegramNotifier, _FakeBotClient, _FakeI18n]:
    bot = _FakeBotClient()
    i18n = _FakeI18n()
    notifier = TelegramNotifier(bot=bot, chat_id=WHITELISTED_CHAT_ID, i18n=i18n, lang="en")
    return notifier, bot, i18n


def test_telegramnotifier_implements_notifier_contract() -> None:
    assert issubclass(TelegramNotifier, Notifier)


async def test_notify_sends_i18n_sourced_text_and_returns_none() -> None:
    notifier, bot, i18n = _make_notifier()

    result = await notifier.notify(NotificationMessage(case_id="case-1", text="hello human"))

    assert result is None
    assert len(bot.sent_messages) == 1
    assert bot.sent_messages[0]["chat_id"] == WHITELISTED_CHAT_ID
    assert i18n.calls[0][0] == "telegram.notification"


# --------------------------------------------------------------------------------------------- #
# R23 -- no literal user-facing string ever reaches `send_message`.
# --------------------------------------------------------------------------------------------- #

_OUTBOUND_TEXT_CALL_NAMES = frozenset({"send_message"})


def _literal_string_violations(module_path: Path) -> list[str]:
    """Every call site in `module_path` sending user-facing text with a literal string.

    Flags any `ast.Constant` string argument (positional or keyword) passed to `send_message` --
    the one call shape that puts text in front of a human in this module. A translated value (the
    result of `self._i18n.translate(...)`, always a `Name`/`Call`/f-string node, never a bare
    string constant) never trips this check.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name not in _OUTBOUND_TEXT_CALL_NAMES:
            continue
        for arg in (*node.args, *(kw.value for kw in node.keywords)):
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                violations.append(
                    f"{module_path}:{node.lineno}: literal string passed to {name}(...): "
                    f"{arg.value!r}"
                )
    return violations


def test_no_literal_user_facing_strings_in_bot_code() -> None:
    violations = _literal_string_violations(TELEGRAM_MODULE_PATH)
    assert not violations, "\n".join(violations)


def test_literal_string_violation_check_catches_a_real_violation(tmp_path: Path) -> None:
    """Prove the check above is load-bearing -- it must actually catch a literal, not vacuously
    pass (mirrors `tests/test_module_test_coverage.py`'s own self-check pattern)."""
    bad_module = tmp_path / "bad_bot.py"
    bad_module.write_text(
        "async def f(bot):\n"
        '    await bot.send_message("chat", "a hardcoded literal, never do this")\n'
    )

    violations = _literal_string_violations(bad_module)

    assert violations
    assert any("hardcoded literal" in v for v in violations)


def test_telegram_module_docstring_names_r15_r23() -> None:
    doc = Path(TELEGRAM_MODULE_PATH).read_text(encoding="utf-8")
    for restriction in ("R15", "R23"):
        assert restriction in doc, (
            f"restriction {restriction!r} not found in {TELEGRAM_MODULE_PATH}"
        )


# --------------------------------------------------------------------------------------------- #
# `PythonTelegramBotClient` -- the real-library boundary. Exercised without ever letting a real
# HTTP request happen: the underlying `telegram.Bot` is swapped for a fake after construction (the
# library performs no network I/O at construction time).
# --------------------------------------------------------------------------------------------- #


class _FakeRealBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, *, chat_id, text):
        self.sent.append({"chat_id": chat_id, "text": text})


async def test_python_telegram_bot_client_send_message() -> None:
    client = PythonTelegramBotClient("123:fake-token-never-used")
    fake_bot = _FakeRealBot()
    client._bot = fake_bot  # swap out the real telegram.Bot -- no network call ever happens

    await client.send_message("111", "hello")

    assert fake_bot.sent == [{"chat_id": "111", "text": "hello"}]
