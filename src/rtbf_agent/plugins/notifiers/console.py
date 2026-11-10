"""`ConsoleNotifier` — a stdout implementation of `core.interfaces.Notifier`.

The simplest possible real notification surface: it prints one-way notices to stdout. Every human
decision this notifier used to solicit live (`request_approval()`, `receive_document()`,
`receive_text_response()`) is gone -- see `Notifier`'s module docstring for why: those decisions
are now recorded via `case_actions.py` (kanban route or CLI subcommand), never by blocking this
notifier on a live response, so there is nothing left for a "wait for stdin" implementation to do.
"""

from __future__ import annotations

from collections.abc import Callable

from rtbf_agent.core.interfaces import NotificationMessage, Notifier


class ConsoleNotifier(Notifier):
    """Prints one-way notices to stdout. No decisions, no queueing."""

    def __init__(self, *, print_fn: Callable[[str], None] = print) -> None:
        # Injected rather than called directly so tests can capture output without needing a real
        # terminal (per issue #9's original verification note).
        self._print = print_fn

    async def notify(self, message: NotificationMessage) -> None:
        """Print a one-way notice. Never solicits or returns a decision (R1)."""
        self._print(f"[case {message.case_id}] {message.text}")
