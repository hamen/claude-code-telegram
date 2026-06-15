"""Tests for the pluggable agent engine layer (factory + Cursor adapter)."""

import os
import stat
import tempfile
import textwrap
from pathlib import Path

import pytest

from src.claude.exceptions import ClaudeTimeoutError
from src.claude.sdk_integration import ClaudeSDKManager, StreamUpdate
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

    def test_prompt_is_separated_by_double_dash(self):
        # A prompt that looks like a flag must be passed as the prompt, not parsed
        # as a cursor-agent option.
        cmd = self._mgr()._build_command("--help me", Path("/w"), None, False)
        assert cmd[-2] == "--"
        assert cmd[-1] == "--help me"

    def test_partial_flag_only_when_requested(self):
        mgr = self._mgr()
        assert "--stream-partial-output" not in mgr._build_command(
            "x", Path("/w"), None, False
        )
        assert "--stream-partial-output" in mgr._build_command(
            "x", Path("/w"), None, False, partial=True
        )

    def test_resume_added_only_when_continuing(self):
        mgr = self._mgr()
        with_resume = mgr._build_command("x", Path("/w"), "sid-123", True)
        assert "--resume" in with_resume
        assert "sid-123" in with_resume

        # No resume when not continuing, or when session id is missing.
        assert "--resume" not in mgr._build_command("x", Path("/w"), "sid-123", False)
        assert "--resume" not in mgr._build_command("x", Path("/w"), None, True)

    def test_approve_mcps_only_when_mcp_enabled(self):
        mcp_cfg = Path(_APPROVED_DIR) / "mcp.json"
        mcp_cfg.write_text('{"mcpServers": {"dummy": {"command": "true"}}}')
        assert "--approve-mcps" not in self._mgr()._build_command(
            "x", Path("/w"), None, False
        )
        with_mcp = self._mgr(enable_mcp=True, mcp_config_path=str(mcp_cfg))
        assert "--approve-mcps" in with_mcp._build_command("x", Path("/w"), None, False)


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


def _fake_cli(body: str) -> str:
    """Write an executable fake cursor-agent that runs `body` (a Python snippet)."""
    fd, path = tempfile.mkstemp(prefix="fake-cursor-", suffix=".py", dir=_APPROVED_DIR)
    script = "#!/usr/bin/env python3\nimport sys, time\n" + textwrap.dedent(body)
    with os.fdopen(fd, "w") as fh:
        fh.write(script)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IRUSR)
    return path


class TestCursorExecuteCommand:
    async def _run(self, cli_path, **cfg):
        mgr = CursorAgentManager(
            _settings(agent_backend="cursor", cursor_agent_path=cli_path, **cfg)
        )
        updates = []

        async def on_stream(u: StreamUpdate):
            updates.append(u)

        resp = await mgr.execute_command(
            prompt="hi",
            working_directory=Path(_APPROVED_DIR),
            stream_callback=on_stream,
        )
        return resp, updates

    async def test_stream_parsing_and_response(self):
        # Mirrors --stream-partial-output: text deltas carry timestamp_ms, then a
        # final cumulative assistant message without it, then the result.
        cli = _fake_cli(r"""
            for line in [
                '{"type":"system","subtype":"init","session_id":"sid-1","model":"Composer 2.5"}',
                '{"type":"tool_call","subtype":"started","tool_call":{"shellToolCall":{"args":{"command":"ls"},"description":"list"}}}',
                '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"hi "}]},"timestamp_ms":1}',
                '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"there"}]},"timestamp_ms":2}',
                '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"hi there"}]}}',
                '{"type":"result","subtype":"success","is_error":false,"result":"hi there","session_id":"sid-1","duration_ms":42}',
            ]:
                print(line, flush=True)
            """)
        resp, updates = await self._run(cli)
        assert resp.content == "hi there"
        assert resp.session_id == "sid-1"
        assert resp.is_error is False
        assert resp.duration_ms == 42
        assert [t["name"] for t in resp.tools_used] == ["Bash"]
        # system + tool_call + 2 stream deltas (final cumulative msg is not emitted)
        assert len(updates) == 4
        assert any(u.tool_calls for u in updates)
        deltas = [u.content for u in updates if u.type == "stream_delta"]
        assert deltas == ["hi ", "there"]

    async def test_tools_only_fallback_message(self):
        cli = _fake_cli(r"""
            for line in [
                '{"type":"system","subtype":"init","session_id":"sid-2"}',
                '{"type":"tool_call","subtype":"started","tool_call":{"writeToolCall":{"args":{}}}}',
                '{"type":"result","subtype":"success","is_error":false,"result":"","session_id":"sid-2"}',
            ]:
                print(line, flush=True)
            """)
        resp, _ = await self._run(cli)
        assert resp.content.startswith("✅ Task completed")
        assert "Write" in resp.content

    async def test_timeout_raises_claude_timeout_error(self):
        cli = _fake_cli("time.sleep(5)\n")
        with pytest.raises(ClaudeTimeoutError):
            await self._run(cli, claude_timeout_seconds=1)


class TestBackendScopedResume:
    """The P1 fix: auto-resume must never cross agent backends."""

    async def test_only_resumes_session_from_matching_backend(self):
        from datetime import UTC, datetime
        from unittest.mock import MagicMock

        from src.claude.facade import ClaudeIntegration
        from src.claude.session import ClaudeSession

        wd = Path(_APPROVED_DIR)
        now = datetime.now(UTC)
        claude_sess = ClaudeSession(
            session_id="claude-1",
            user_id=1,
            project_path=wd,
            created_at=now,
            last_used=now,
            backend="claude",
        )
        cursor_sess = ClaudeSession(
            session_id="cursor-1",
            user_id=1,
            project_path=wd,
            created_at=now,
            last_used=now,
            backend="cursor",
        )

        sm = MagicMock()

        async def _get_user_sessions(_uid):
            return [claude_sess, cursor_sess]

        sm._get_user_sessions = _get_user_sessions

        integ = ClaudeIntegration(
            config=_settings(agent_backend="cursor"),
            sdk_manager=MagicMock(),
            session_manager=sm,
        )
        found = await integ._find_resumable_session(1, wd)
        assert found is not None and found.session_id == "cursor-1"

        integ_claude = ClaudeIntegration(
            config=_settings(agent_backend="claude"),
            sdk_manager=MagicMock(),
            session_manager=sm,
        )
        found_claude = await integ_claude._find_resumable_session(1, wd)
        assert found_claude is not None and found_claude.session_id == "claude-1"
