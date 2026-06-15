"""Agent engine abstraction.

Defines the common contract every agent backend (Claude, Cursor, OpenCode, …)
must satisfy so the rest of the bot stays engine-agnostic. Handlers only ever
talk to an object implementing :class:`AgentManager`; swapping engines is a
config change (``AGENT_BACKEND``), not a code change.

The shared response/stream contract is intentionally reused from the existing
Claude integration (:class:`ClaudeResponse` / :class:`StreamUpdate`) so that no
call site in the bot needs to change. They are re-exported here under
engine-neutral aliases for new code to use.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, runtime_checkable

from ..claude.sdk_integration import ClaudeResponse, StreamUpdate

# Engine-neutral aliases for the shared contract. New code should prefer these
# names; the underlying dataclasses live in ``src/claude/sdk_integration`` for
# backward compatibility with existing imports.
AgentResponse = ClaudeResponse

__all__ = ["AgentManager", "AgentResponse", "StreamUpdate"]


@runtime_checkable
class AgentManager(Protocol):
    """Contract implemented by every agent backend.

    Mirrors the call surface the bot already used on ``ClaudeSDKManager`` so the
    facade (:class:`src.claude.facade.ClaudeIntegration`) can drive any engine.
    """

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
        interrupt_event: Optional[asyncio.Event] = None,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> AgentResponse:
        """Run one agent turn and return the final response.

        Implementations should emit incremental :class:`StreamUpdate` objects via
        ``stream_callback`` (when provided) and honour ``interrupt_event`` by
        aborting the in-flight run.
        """
        ...
