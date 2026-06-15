"""Tests for backend-aware /model selection (Claude vs Cursor)."""

import tempfile

from src.bot.handlers.command import model_config_for_backend
from src.config.settings import Settings

_APPROVED_DIR = tempfile.mkdtemp(prefix="model-test-")


def _settings(**overrides) -> Settings:
    base = {
        "telegram_bot_token": "test_token",
        "telegram_bot_username": "test_bot",
        "approved_directory": _APPROVED_DIR,
        "allowed_users": [1],
    }
    base.update(overrides)
    return Settings(**base)


def test_claude_backend_lists_claude_models():
    models, _current, env_key, label = model_config_for_backend(_settings())
    assert env_key == "CLAUDE_MODEL"
    assert label == "Claude"
    assert all(mid.startswith("claude-") for mid, _ in models)


def test_cursor_backend_lists_curated_cursor_models():
    models, current, env_key, label = model_config_for_backend(
        _settings(agent_backend="cursor", cursor_model="composer-2.5")
    )
    assert env_key == "CURSOR_MODEL"
    assert label == "Cursor"
    assert current == "composer-2.5"
    assert [mid for mid, _ in models] == [
        "composer-2.5",
        "gpt-5.3-codex",
        "gemini-3.5-flash",
    ]
