"""Tests for `rtbf_agent.plugins.notifiers.console.ConsoleNotifier`.

Verifies it correctly implements the `Notifier` contract from `core/interfaces.py` -- now
notify-only (automated-case-loop rework; see `Notifier`'s module docstring for why the old
approval/document/text-response solicitation was removed).
"""

from __future__ import annotations

from rtbf_agent.core.interfaces import NotificationMessage, Notifier
from rtbf_agent.plugins.notifiers.console import ConsoleNotifier


class _FakeIO:
    """Records printed lines."""

    def __init__(self) -> None:
        self.printed: list[str] = []

    def print_fn(self, text: str) -> None:
        self.printed.append(text)


def test_consolenotifier_implements_notifier_contract() -> None:
    assert issubclass(ConsoleNotifier, Notifier)


async def test_notify_prints_message_and_returns_none() -> None:
    io = _FakeIO()
    notifier = ConsoleNotifier(print_fn=io.print_fn)

    result = await notifier.notify(NotificationMessage(case_id="case-1", text="hello human"))

    assert result is None
    assert any("case-1" in line and "hello human" in line for line in io.printed)


def test_consolenotifier_is_concrete_class() -> None:
    """Sanity check that it's a concrete class, not still abstract."""
    notifier = ConsoleNotifier(print_fn=lambda _text: None)
    assert callable(notifier.notify)
