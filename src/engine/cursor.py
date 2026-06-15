"""Cursor Agent backend.

Drives Cursor's headless ``cursor-agent`` CLI and adapts its ``stream-json``
event stream to the bot's engine contract (:class:`AgentManager`). Runs on the
user's Cursor subscription (e.g. Composer 2.5) instead of a metered API key.

Event schema produced by ``cursor-agent -p --output-format stream-json``
(one JSON object per line):

    {"type":"system","subtype":"init","session_id":…,"model":…}
    {"type":"user","message":{…}}
    {"type":"tool_call","subtype":"started"|"completed","tool_call":{"<kind>ToolCall":{…}}}
    {"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":…}]}}
    {"type":"result","subtype":"success","is_error":false,"result":…,"session_id":…,"usage":{…}}
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

from ..claude.sdk_integration import ClaudeResponse, StreamUpdate
from ..config.settings import Settings
from ..security.validators import SecurityValidator

logger = structlog.get_logger()

# Max bytes per stream-json line. Tool-call events embed (capped) command
# output, so keep this generous.
_STREAM_LIMIT = 16 * 1024 * 1024

# Map Cursor tool kinds to Claude-style names so the bot's existing tool icons
# and rendering keep working. Unknown kinds fall back to a capitalised name.
_TOOL_NAME_MAP: Dict[str, str] = {
    "shell": "Bash",
    "read": "Read",
    "readFile": "Read",
    "write": "Write",
    "writeFile": "Write",
    "edit": "Edit",
    "applyPatch": "Edit",
    "search": "Grep",
    "grep": "Grep",
    "codebaseSearch": "Grep",
    "semSearch": "Grep",
    "ls": "LS",
    "list": "LS",
    "glob": "Glob",
    "delete": "Bash",
    "todo": "TodoWrite",
    "webSearch": "WebSearch",
    "fetch": "WebFetch",
    "mcp": "MCP",
}


class CursorAgentManager:
    """Agent backend backed by the ``cursor-agent`` CLI."""

    def __init__(
        self,
        config: Settings,
        security_validator: Optional[SecurityValidator] = None,
    ) -> None:
        self.config = config
        # Retained for interface parity with ClaudeSDKManager. cursor-agent
        # enforces its own workspace sandbox; per-tool validation is not wired
        # because headless cursor-agent does not expose per-tool callbacks.
        self.security_validator = security_validator
        self.cli_path = getattr(config, "cursor_agent_path", None) or "cursor-agent"
        self.model = getattr(config, "cursor_model", None) or "composer-2.5"

    @staticmethod
    def _tool_name(kind: str) -> str:
        return _TOOL_NAME_MAP.get(kind, kind[:1].upper() + kind[1:] if kind else "Tool")

    @staticmethod
    async def _emit(
        stream_callback: Optional[Callable[[StreamUpdate], Any]],
        update: StreamUpdate,
    ) -> None:
        if stream_callback is None:
            return
        try:
            result = stream_callback(update)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # never let UI streaming break the run
            logger.warning(
                "Cursor stream callback failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def _build_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str],
        continue_session: bool,
    ) -> List[str]:
        cmd = [
            self.cli_path,
            "-p",
            "--output-format",
            "stream-json",
            "--model",
            self.model,
            "--workspace",
            str(working_directory),
            "--force",  # auto-approve tools (no interactive per-tool prompts in headless)
            "--trust",  # trust the workspace without prompting
        ]
        if continue_session and session_id:
            cmd += ["--resume", session_id]
        # Prompt is the trailing positional argument.
        cmd.append(prompt)
        return cmd

    @staticmethod
    def _parse_tool_call(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Extract a {name, input} dict from a tool_call event, or None."""
        tool_call = event.get("tool_call") or {}
        for key, inner in tool_call.items():
            if not key.endswith("ToolCall") or not isinstance(inner, dict):
                continue
            kind = key[: -len("ToolCall")]
            args = inner.get("args") if isinstance(inner.get("args"), dict) else {}
            return {
                "name": CursorAgentManager._tool_name(kind),
                "input": args or {},
                "description": inner.get("description")
                or (args or {}).get("command")
                or "",
            }
        return None

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
        interrupt_event: Optional[asyncio.Event] = None,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> ClaudeResponse:
        start_time = asyncio.get_event_loop().time()

        if images:
            logger.warning(
                "Cursor backend does not support image inputs; ignoring images",
                count=len(images),
            )

        cmd = self._build_command(
            prompt, working_directory, session_id, continue_session
        )
        logger.info(
            "Starting cursor-agent command",
            working_directory=str(working_directory),
            session_id=session_id,
            continue_session=continue_session,
            model=self.model,
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(working_directory),
            env=os.environ.copy(),
            limit=_STREAM_LIMIT,
        )

        interrupted = False
        final_text = ""
        result_session_id: Optional[str] = None
        is_error = False
        error_type: Optional[str] = None
        result_duration_ms: Optional[int] = None
        tools_used: List[Dict[str, Any]] = []
        assistant_turns = 0

        async def _watch_interrupt() -> None:
            nonlocal interrupted
            assert interrupt_event is not None
            await interrupt_event.wait()
            interrupted = True
            if proc.returncode is None:
                proc.kill()

        interrupt_task: Optional[asyncio.Task] = None
        if interrupt_event is not None:
            interrupt_task = asyncio.create_task(_watch_interrupt())

        async def _consume() -> None:
            nonlocal final_text, result_session_id, is_error, error_type
            nonlocal result_duration_ms, assistant_turns
            assert proc.stdout is not None
            while True:
                try:
                    raw = await proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError) as exc:
                    # Oversized line — skip it and keep going.
                    logger.warning(
                        "Skipping oversized cursor-agent line", error=str(exc)
                    )
                    continue
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type")

                if etype == "system" and event.get("subtype") == "init":
                    result_session_id = event.get("session_id") or result_session_id
                    await self._emit(
                        stream_callback,
                        StreamUpdate(
                            type="system",
                            metadata={
                                "session_id": event.get("session_id"),
                                "model": event.get("model"),
                            },
                        ),
                    )
                elif etype == "tool_call" and event.get("subtype") == "started":
                    parsed = self._parse_tool_call(event)
                    if parsed:
                        tools_used.append(
                            {
                                "name": parsed["name"],
                                "timestamp": asyncio.get_event_loop().time(),
                                "input": parsed["input"],
                            }
                        )
                        await self._emit(
                            stream_callback,
                            StreamUpdate(
                                type="assistant",
                                tool_calls=[
                                    {"name": parsed["name"], "input": parsed["input"]}
                                ],
                            ),
                        )
                elif etype == "assistant":
                    text = _extract_text(event.get("message"))
                    if text:
                        assistant_turns += 1
                        await self._emit(
                            stream_callback,
                            StreamUpdate(type="assistant", content=text),
                        )
                elif etype == "result":
                    final_text = event.get("result") or final_text
                    result_session_id = event.get("session_id") or result_session_id
                    is_error = bool(event.get("is_error"))
                    if is_error:
                        error_type = event.get("subtype") or "error"
                    if isinstance(event.get("duration_ms"), int):
                        result_duration_ms = event.get("duration_ms")
                    break

        timeout = getattr(self.config, "claude_timeout_seconds", 0) or None
        try:
            try:
                await asyncio.wait_for(_consume(), timeout=timeout)
            except asyncio.TimeoutError:
                if proc.returncode is None:
                    proc.kill()
                stderr = await _read_all(proc.stderr)
                raise TimeoutError(
                    f"cursor-agent timed out after {timeout}s"
                    + (f": {stderr.strip()}" if stderr else "")
                )

            await proc.wait()

            if proc.returncode not in (0, None) and not interrupted and not final_text:
                stderr = await _read_all(proc.stderr)
                raise RuntimeError(
                    f"cursor-agent exited with code {proc.returncode}"
                    + (f": {stderr.strip()}" if stderr else "")
                )
        finally:
            if interrupt_task is not None:
                interrupt_task.cancel()
            if proc.returncode is None:
                proc.kill()

        duration_ms = result_duration_ms
        if duration_ms is None:
            duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)

        final_session_id = result_session_id or session_id or ""

        logger.info(
            "cursor-agent command completed",
            session_id=final_session_id,
            duration_ms=duration_ms,
            num_turns=assistant_turns,
            is_error=is_error,
            interrupted=interrupted,
        )

        return ClaudeResponse(
            content=final_text,
            session_id=final_session_id,
            cost=0.0,  # Cursor runs on the subscription; no per-request USD cost.
            duration_ms=duration_ms,
            num_turns=max(assistant_turns, 1),
            is_error=is_error,
            error_type=error_type,
            tools_used=tools_used,
            interrupted=interrupted,
        )


def _extract_text(message: Optional[Dict[str, Any]]) -> str:
    """Pull concatenated text from an assistant message's content blocks."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if text:
                parts.append(text)
    return "".join(parts)


async def _read_all(stream: Optional[asyncio.StreamReader]) -> str:
    if stream is None:
        return ""
    try:
        data = await stream.read()
    except Exception:
        return ""
    return data.decode("utf-8", errors="replace")
