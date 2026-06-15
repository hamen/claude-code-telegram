"""Agent engine registry and factory.

The bot is engine-agnostic: it talks to an :class:`~src.engine.base.AgentManager`
without knowing whether Claude, Cursor, or some other agent is behind it. Which
engine is used is selected at startup by the ``AGENT_BACKEND`` setting.

Adding a new backend (e.g. OpenCode) is two steps:
    1. Write a manager satisfying :class:`AgentManager` (see ``cursor.py``).
    2. Register a builder for it in ``_BACKENDS`` below.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

import structlog

from ..config.settings import Settings
from ..security.validators import SecurityValidator
from .base import AgentManager, AgentResponse, StreamUpdate

logger = structlog.get_logger()

# Builder signature: (config, security_validator) -> AgentManager
_Builder = Callable[[Settings, Optional[SecurityValidator]], AgentManager]


def _build_claude(
    config: Settings, security_validator: Optional[SecurityValidator]
) -> AgentManager:
    from ..claude.sdk_integration import ClaudeSDKManager

    return ClaudeSDKManager(config, security_validator=security_validator)


def _build_cursor(
    config: Settings, security_validator: Optional[SecurityValidator]
) -> AgentManager:
    from .cursor import CursorAgentManager

    return CursorAgentManager(config, security_validator=security_validator)


# Registry of known backends. Keep keys lowercase; they map directly to the
# ``AGENT_BACKEND`` config value.
_BACKENDS: Dict[str, _Builder] = {
    "claude": _build_claude,
    "cursor": _build_cursor,
}


def available_backends() -> List[str]:
    """Return the list of registered backend names."""
    return sorted(_BACKENDS)


def create_agent_manager(
    config: Settings,
    security_validator: Optional[SecurityValidator] = None,
) -> AgentManager:
    """Instantiate the agent manager selected by ``config.agent_backend``.

    Falls back to the Claude backend when the setting is unset.
    """
    backend = (getattr(config, "agent_backend", "claude") or "claude").strip().lower()
    builder = _BACKENDS.get(backend)
    if builder is None:
        raise ValueError(
            f"Unknown AGENT_BACKEND {backend!r}; "
            f"supported backends: {', '.join(available_backends())}"
        )

    logger.info("Creating agent manager", backend=backend)
    return builder(config, security_validator)


__all__ = [
    "AgentManager",
    "AgentResponse",
    "StreamUpdate",
    "available_backends",
    "create_agent_manager",
]
