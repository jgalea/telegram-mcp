"""Single source of truth for tool tiers and the runtime policy.

Lives in its own module so both server.py (proxy) and daemon.py can import
without creating a circular dependency through server's existing import of
daemon.SOCKET_PATH.

Tiers:

- DESTRUCTIVE_TOOLS: need ``confirm: true`` (a JSON boolean) or the daemon
  returns a warning instead of executing.
- WRITE_TOOLS: anything that mutates Telegram account state. Hidden in
  read-only mode, counted against the hourly write budget, and written to
  the audit log.
- PEER_SEND_TOOLS: write tools that deliver content to a peer, mapped to the
  argument that names the peer. Subject to ``send_allowlist``.
- LOCAL_ONLY_TOOLS: never talk to Telegram (cache and status tools).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DESTRUCTIVE_TOOLS: frozenset[str] = frozenset({
    "delete_chat",
    "leave_chat",
    "block_user",
    "remove_participant",
    "delete_message",
    "forward_message",
    "get_invite_link",
    "add_participant",
    "create_channel",
    "create_group",
    "set_chat_title",
    "set_chat_description",
    "set_chat_photo",
})

PEER_SEND_TOOLS: dict[str, str] = {
    "send_message": "chat_id",
    "schedule_message": "chat_id",
    "edit_message": "chat_id",
    "send_reaction": "chat_id",
    "send_file": "chat_id",
    "send_voice": "chat_id",
    "send_location": "chat_id",
    "forward_message": "to_chat",
}

WRITE_TOOLS: frozenset[str] = DESTRUCTIVE_TOOLS | frozenset(PEER_SEND_TOOLS) | frozenset({
    "archive_chat",
    "mute_chat",
    "mark_read",
    "pin_message",
    "unpin_message",
    "unblock_user",
})

# Mutates local state only; still hidden in read-only mode.
LOCAL_MUTATING_TOOLS: frozenset[str] = frozenset({"clear_cache"})

LOCAL_ONLY_TOOLS: frozenset[str] = frozenset({
    "search_regex",
    "chat_analytics",
    "message_timeline",
    "today_messages",
    "export_cached_messages",
    "clear_cache",
    "get_status",
})

# Argument that names the peer for audit purposes, for write tools that are
# not in PEER_SEND_TOOLS.
_PEER_ARGS: dict[str, str] = {
    "block_user": "user_id",
    "unblock_user": "user_id",
    "remove_participant": "user_id",
    "add_participant": "user_id",
}

# Argument carrying free text worth recording in the audit log.
_TEXT_ARGS: dict[str, str] = {
    "send_message": "text",
    "schedule_message": "text",
    "edit_message": "text",
    "send_file": "caption",
    "send_reaction": "emoji",
    "set_chat_title": "title",
    "set_chat_description": "description",
    "create_group": "title",
    "create_channel": "title",
}


def is_read_only_tool(name: str) -> bool:
    return name not in WRITE_TOOLS and name not in LOCAL_MUTATING_TOOLS


def peer_arg(tool: str) -> str | None:
    return PEER_SEND_TOOLS.get(tool) or _PEER_ARGS.get(tool) or (
        "chat_id" if tool in WRITE_TOOLS else None
    )


def text_arg(tool: str) -> str | None:
    return _TEXT_ARGS.get(tool)


READ_ONLY_ENV = "TELEGRAM_MCP_READ_ONLY"
DEFAULT_WRITE_PER_HOUR = 200


def env_read_only(environ: dict[str, str] | None = None) -> bool:
    val = (environ if environ is not None else os.environ).get(READ_ONLY_ENV, "")
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Policy:
    """Runtime policy from config.json and the environment. Not tool-settable."""

    read_only: bool = False
    send_allowlist: frozenset[int | str] | None = None
    write_per_hour: int = DEFAULT_WRITE_PER_HOUR

    def peer_allowed(self, peer: object) -> bool:
        if self.send_allowlist is None:
            return True
        return _normalize_peer(peer) in self.send_allowlist


def _normalize_peer(value: object) -> int | str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    return value.lower() if value.startswith("@") else f"@{value.lower()}"


def policy_from_config(config: dict, environ: dict[str, str] | None = None) -> Policy:
    read_only = env_read_only(environ) or config.get("mode") == "read_only"

    allowlist: frozenset[int | str] | None = None
    raw = config.get("send_allowlist")
    if raw is not None:
        if not isinstance(raw, list):
            raise ValueError("send_allowlist must be a list of chat ids or @usernames")
        normalized = {_normalize_peer(v) for v in raw}
        normalized.discard(None)
        allowlist = frozenset(normalized)

    per_hour = config.get("rate_limits", {}).get("write_per_hour", DEFAULT_WRITE_PER_HOUR)
    per_hour = int(per_hour)
    if per_hour < 1:
        raise ValueError("rate_limits.write_per_hour must be at least 1")

    return Policy(read_only=read_only, send_allowlist=allowlist, write_per_hour=per_hour)


def load_policy() -> Policy:
    from telegram_mcp.login import load_config  # noqa: PLC0415 - avoid import cycle

    return policy_from_config(load_config())
