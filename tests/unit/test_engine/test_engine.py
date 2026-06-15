"""Tests for the pluggable agent engine layer (factory + Cursor adapter)."""

import tempfile
from pathlib import Path

import pytest

from src.claude.sdk_integration import ClaudeSDKManager
from src.config.settings import Settings
from src.engine import available_backends, create_agent_manager
from src.engine.cursor import CursorAgentManager, _extract_text

# Settings validates that approved_directory exists, so use a real temp dir.
_APPROVED_DIR = tempfile.mkdtemp(prefix="engine-test-")


def _settings(**overrides) -> Settings:
    base = {
        "telegram_bot_token": "test_token",
        "telegram_bot_username": "test_bot",
        "approved_directory": _APPROVED_DIR,
        "allowed_users": [123456789],
    }
    base.update(overrides)
    return Settings(**base)


class TestFactory:
    def test_available_backends(self):
        assert set(available_backends()) >= {"claude", "cursor"}

    def test_default_backend_is_claude(self):
        mgr = create_agent_manager(_settings())
        assert isinstance(mgr, ClaudeSDKManager)

    def test_cursor_backend_selected(self):
        mgr = create_agent_manager(_settings(agent_backend="cursor"))
        assert isinstance(mgr, CursorAgentManager)

    def test_backend_name_is_case_insensitive(self):
        mgr = create_agent_manager(_settings(agent_backend="CURSOR"))
        assert isinstance(mgr, CursorAgentManager)

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown AGENT_BACKEND"):
            create_agent_manager(_settings(agent_backend="bogus"))


class TestCursorBuildCommand:
    def _mgr(self, **overrides) -> CursorAgentManager:
        return CursorAgentManager(_settings(agent_backend="cursor", **overrides))

    def test_core_flags_present(self):
        cmd = self._mgr()._build_command(
            "hello", Path("/work/dir"), session_id=None, continue_session=False
        )
        assert cmd[0] == "cursor-agent"
        assert "-p" in cmd
        assert "--output-format" in cmd and "stream-json" in cmd
        assert "--model" in cmd and "composer-2.5" in cmd
        assert "--workspace" in cmd and "/work/dir" in cmd
        assert "--force" in cmd and "--trust" in cmd
        # Prompt is the trailing positional argument.
        assert cmd[-1] == "hello"

    def test_custom_model_and_path(self):
        cmd = self._mgr(
            cursor_model="gpt-5.3-codex", cursor_agent_path="/opt/cursor-agent"
        )._build_command("x", Path("/w"), None, False)
        assert cmd[0] == "/opt/cursor-agent"
        assert "gpt-5.3-codex" in cmd

    def test_resume_added_only_when_continuing(self):
        mgr = self._mgr()
        with_resume = mgr._build_command("x", Path("/w"), "sid-123", True)
        assert "--resume" in with_resume
        assert "sid-123" in with_resume

        # No resume when not continuing, or when session id is missing.
        assert "--resume" not in mgr._build_command("x", Path("/w"), "sid-123", False)
        assert "--resume" not in mgr._build_command("x", Path("/w"), None, True)


class TestToolName:
    def test_known_kinds_map_to_claude_names(self):
        assert CursorAgentManager._tool_name("shell") == "Bash"
        assert CursorAgentManager._tool_name("read") == "Read"
        assert CursorAgentManager._tool_name("write") == "Write"

    def test_unknown_kind_is_capitalised(self):
        assert CursorAgentManager._tool_name("frobnicate") == "Frobnicate"

    def test_empty_kind_falls_back(self):
        assert CursorAgentManager._tool_name("") == "Tool"


class TestParseToolCall:
    def test_shell_tool_call(self):
        event = {
            "type": "tool_call",
            "subtype": "started",
            "tool_call": {
                "shellToolCall": {
                    "args": {"command": "ls -la"},
                    "description": "List files",
                }
            },
        }
        parsed = CursorAgentManager._parse_tool_call(event)
        assert parsed is not None
        assert parsed["name"] == "Bash"
        assert parsed["input"] == {"command": "ls -la"}

    def test_unknown_tool_kind(self):
        event = {"tool_call": {"customToolCall": {"args": {"x": 1}}}}
        parsed = CursorAgentManager._parse_tool_call(event)
        assert parsed["name"] == "Custom"
        assert parsed["input"] == {"x": 1}

    def test_no_tool_call_returns_none(self):
        assert CursorAgentManager._parse_tool_call({"type": "assistant"}) is None
        assert CursorAgentManager._parse_tool_call({"tool_call": {}}) is None


class TestExtractText:
    def test_text_blocks_concatenated(self):
        msg = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Hello "},
                {"type": "text", "text": "world"},
                {"type": "other", "text": "ignored"},
            ],
        }
        assert _extract_text(msg) == "Hello world"

    def test_string_content(self):
        assert _extract_text({"content": "plain"}) == "plain"

    def test_missing_or_invalid(self):
        assert _extract_text(None) == ""
        assert _extract_text({}) == ""
        assert _extract_text({"content": 123}) == ""
