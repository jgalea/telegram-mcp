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

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

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

# Only these spellings turn the env var off. Anything else that is set,
# including typos like "enabled", counts as on: the variable exists to
# restrict, so an unrecognised value must not silently widen access.
_ENV_FALSE = frozenset({"", "0", "false", "no", "off"})
_MODE_FULL = frozenset({"", "full", "read_write", "readwrite", "rw"})
_MODE_READ_ONLY = frozenset({"read_only", "readonly", "ro"})


def env_read_only(environ: dict[str, str] | None = None) -> bool:
    env = environ if environ is not None else os.environ
    if READ_ONLY_ENV not in env:
        return False
    return env[READ_ONLY_ENV].strip().lower() not in _ENV_FALSE


def _mode_read_only(mode: object) -> bool:
    if mode is None:
        return False
    if isinstance(mode, bool):
        return mode
    key = str(mode).strip().lower().replace("-", "_").replace(" ", "_")
    if key in _MODE_FULL:
        return False
    if key in _MODE_READ_ONLY:
        return True
    logger.error("config.json: unknown mode %r; treating it as read_only", mode)
    return True


@dataclass(frozen=True)
class Policy:
    """Runtime policy from config.json and the environment. Not tool-settable.

    ``send_allowlist`` holds the raw entries (ints or strings) as written in
    config.json. The daemon resolves them to Telegram peer ids through
    Telethon and compares resolved ids, never the spellings: a peer has many
    spellings (``@Name``, ``name``, ``t.me/name``, a phone number, a bare id,
    a ``-100`` channel id) and Telethon accepts all of them, so a string
    comparison can neither equate the same peer nor tell two apart.
    """

    read_only: bool = False
    send_allowlist: tuple[int | str, ...] | None = None
    write_per_hour: int = DEFAULT_WRITE_PER_HOUR

    @property
    def allowlist_active(self) -> bool:
        return self.send_allowlist is not None


FAIL_CLOSED_POLICY = Policy(read_only=True, send_allowlist=(), write_per_hour=1)


def _clean_allowlist(raw: object) -> tuple[int | str, ...]:
    if not isinstance(raw, list):
        raise ValueError("send_allowlist must be a list of chat ids or @usernames")
    entries: list[int | str] = []
    for item in raw:
        if isinstance(item, bool):
            raise ValueError(f"send_allowlist entry {item!r} is not a chat id or username")
        if isinstance(item, int):
            entries.append(item)
        elif isinstance(item, str) and item.strip():
            entries.append(item.strip())
        else:
            raise ValueError(f"send_allowlist entry {item!r} is not a chat id or username")
    return tuple(entries)


def policy_from_config(config: dict, environ: dict[str, str] | None = None) -> Policy:
    if not isinstance(config, dict):
        raise ValueError("config.json must contain a JSON object")

    read_only = env_read_only(environ) or _mode_read_only(config.get("mode"))

    allowlist: tuple[int | str, ...] | None = None
    if config.get("send_allowlist") is not None:
        allowlist = _clean_allowlist(config["send_allowlist"])

    rate_limits = config.get("rate_limits") or {}
    if not isinstance(rate_limits, dict):
        raise ValueError("rate_limits must be an object")
    per_hour = rate_limits.get("write_per_hour", DEFAULT_WRITE_PER_HOUR)
    if isinstance(per_hour, bool) or not isinstance(per_hour, (int, float)) or per_hour < 1:
        raise ValueError("rate_limits.write_per_hour must be a number of at least 1")

    return Policy(read_only=read_only, send_allowlist=allowlist, write_per_hour=int(per_hour))


def load_policy() -> Policy:
    """Policy from ~/.telegram-mcp/config.json plus the environment.

    Fails closed: if the config cannot be read or parsed, the result is
    read-only with an empty allowlist, and the error is logged.
    """
    from telegram_mcp.login import load_config  # noqa: PLC0415 - avoid import cycle

    try:
        return policy_from_config(load_config())
    except Exception:
        logger.exception("Could not load policy from config.json; running fail-closed")
        return FAIL_CLOSED_POLICY
