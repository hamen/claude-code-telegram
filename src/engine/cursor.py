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

from ..claude.exceptions import ClaudeTimeoutError
from ..claude.sdk_integration import TASK_COMPLETED_MSG, ClaudeResponse, StreamUpdate
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
        partial: bool = False,
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
        if partial:
            # Emit incremental text deltas so the bot's draft streamer can show
            # the response body as it is generated (not just at the end).
            cmd.append("--stream-partial-output")
        if getattr(self.config, "enable_mcp", False):
            # Headless runs can't answer interactive MCP-approval prompts, so
            # auto-approve the MCP servers configured in cursor-agent's config.
            cmd.append("--approve-mcps")
        if continue_session and session_id:
            cmd += ["--resume", session_id]
        # `--` terminates option parsing so a user prompt that happens to start
        # with '-'/'--' (e.g. "--help", "-v") is treated as the prompt, not a flag.
        cmd.append("--")
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
            prompt,
            working_directory,
            session_id,
            continue_session,
            partial=stream_callback is not None,
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

        # Drain stderr concurrently: a long run that logs heavily to stderr would
        # otherwise fill the OS pipe buffer and block the child while we block on
        # stdout — a classic deadlock. We keep the last lines for diagnostics.
        stderr_chunks: List[str] = []

        async def _drain_stderr() -> None:
            if proc.stderr is None:
                return
            while True:
                try:
                    line = await proc.stderr.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    continue
                if not line:
                    break
                stderr_chunks.append(line.decode("utf-8", errors="replace"))

        stderr_task = asyncio.create_task(_drain_stderr())

        def _stderr_tail() -> str:
            return "".join(stderr_chunks[-20:]).strip()

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
                    # Oversized line (> _STREAM_LIMIT). readline() leaves the data
                    # in the buffer, so a bare `continue` would spin on the same
                    # bytes. Drain a chunk to resync on the next newline instead.
                    logger.warning(
                        "Skipping oversized cursor-agent line", error=str(exc)
                    )
                    try:
                        await proc.stdout.read(_STREAM_LIMIT)
                    except Exception:
                        break
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
                    if not text:
                        continue
                    if "timestamp_ms" in event:
                        # Incremental delta (only with --stream-partial-output):
                        # feed the draft streamer, which appends stream_delta text.
                        await self._emit(
                            stream_callback,
                            StreamUpdate(type="stream_delta", content=text),
                        )
                    else:
                        # Final cumulative assistant message for this turn. The
                        # body was already streamed via deltas (and the final text
                        # comes from the result event), so just count the turn.
                        assistant_turns += 1
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
                    await _reap(proc)
                # Raise the backend-neutral timeout type the bot already renders
                # specially (parity with the Claude backend).
                raise ClaudeTimeoutError(
                    f"cursor-agent timed out after {timeout}s"
                    + (f": {_stderr_tail()}" if _stderr_tail() else "")
                )

            await proc.wait()

            if proc.returncode not in (0, None) and not interrupted and not final_text:
                raise RuntimeError(
                    f"cursor-agent exited with code {proc.returncode}"
                    + (f": {_stderr_tail()}" if _stderr_tail() else "")
                )
        finally:
            # Guarantee the child is reaped (no zombies) and helper tasks stop.
            if proc.returncode is None:
                proc.kill()
                await _reap(proc)
            for task in (interrupt_task, stderr_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *[t for t in (interrupt_task, stderr_task) if t is not None],
                return_exceptions=True,
            )

        # Mirror the Claude backend: if tools ran but no text came back, don't
        # hand the user a blank message.
        if not final_text and tools_used:
            unique = list(dict.fromkeys(t["name"] for t in tools_used))
            final_text = TASK_COMPLETED_MSG.format(tools_summary=", ".join(unique))

        # Surface dropped images to the user instead of silently answering from
        # text only (the Cursor backend cannot accept image inputs).
        if images and not is_error:
            final_text = (
                "⚠️ The Cursor backend can't read images, so I answered from the "
                "text only.\n\n" + final_text
            )

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


async def _reap(proc: "asyncio.subprocess.Process") -> None:
    """Wait for a killed process to exit so it is not left as a zombie."""
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except Exception:
        # Best-effort: never let cleanup raise out of execute_command.
        pass
