from decimal import Decimal

import pytest

from chess_agents.config import Settings, username


def test_offline_defaults(monkeypatch):
    for key in [
        "OPENAI_API_KEY",
        "OPENAI_API_KEY_FILE",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN_FILE",
        "OPENAI_MODEL",
    ]:
        monkeypatch.delenv(key, raising=False)
    assert Settings.load().budget == Decimal("0.01")
    with pytest.raises(ValueError, match="Bot requires"):
        Settings.load().require_bot()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("OPENAI_MODEL", "other"),
        ("CHESS_BUDGET_USD", "0.011"),
        ("CHESS_BUDGET_USD", "NaN"),
        ("CHESS_DEADLINE_SECONDS", "301"),
        ("CHESS_DEADLINE_SECONDS", "nan"),
        ("TELEGRAM_ALLOWED_USER_ID", "-1"),
    ],
)
def test_unsafe_settings(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        Settings.load()


def test_secret_conflict_never_discloses(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-value")
    monkeypatch.setenv("OPENAI_API_KEY_FILE", "/missing")
    with pytest.raises(ValueError) as caught:
        Settings.load()
    assert "secret-value" not in str(caught.value)


@pytest.mark.parametrize("value", ["https://bad", "8/8/8/8", "a b", "../escape", "ab"])
def test_username_boundary(value):
    with pytest.raises(ValueError):
        username(value)
