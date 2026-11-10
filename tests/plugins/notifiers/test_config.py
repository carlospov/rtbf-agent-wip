"""Tests for `rtbf_agent.plugins.notifiers.config` -- Telegram bot token + whitelist `chat_id`
resolution (R14, R15).
"""

from __future__ import annotations

import pytest

from rtbf_agent.plugins.notifiers import config as config_module


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    yield


def test_resolve_telegram_config_raises_when_both_missing() -> None:
    with pytest.raises(LookupError, match="TELEGRAM_BOT_TOKEN"):
        config_module.resolve_telegram_config()


def test_resolve_telegram_config_raises_naming_only_the_missing_var(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    with pytest.raises(LookupError, match="TELEGRAM_CHAT_ID"):
        config_module.resolve_telegram_config()


def test_resolve_telegram_config_from_env_vars(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987654321")

    resolved = config_module.resolve_telegram_config()

    assert resolved.bot_token == "123:abc-token"
    assert resolved.chat_id == "987654321"


def test_resolve_telegram_config_treats_blank_values_as_missing(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "   ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987654321")
    with pytest.raises(LookupError, match="TELEGRAM_BOT_TOKEN"):
        config_module.resolve_telegram_config()


def test_telegram_config_repr_never_includes_bot_token(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "super-secret-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987654321")

    resolved = config_module.resolve_telegram_config()

    assert "super-secret-token" not in repr(resolved)
