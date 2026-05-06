"""Single source of truth for the destructive-tool gate.

Lives in its own module so both server.py (proxy) and daemon.py can import
without creating a circular dependency through server's existing import of
daemon.SOCKET_PATH.
"""

from __future__ import annotations

DESTRUCTIVE_TOOLS: frozenset[str] = frozenset({
    "delete_chat",
    "leave_chat",
    "block_user",
    "remove_participant",
    "delete_message",
    "forward_message",
})
