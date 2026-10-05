"""Long-lived daemon that owns the Telethon session.

One daemon process per machine holds the Telethon SQLite session, the message
cache, and the auto-cache event listener. Multiple `telegram-mcp serve`
processes (one per Claude Code session) connect to it as thin stdio→socket
proxies, eliminating SQLite contention from concurrent Telethon clients.

Wire protocol (line-delimited JSON over a Unix socket):

  request:  {"id": <any>, "tool": "<name>", "args": {...}}\\n
  response: {"id": <same>, "result": <any>}\\n
         or {"id": <same>, "error": "<message>"}\\n

Every TelegramMCPClient method is exposed by name; the daemon resolves
arguments via getattr(client, tool)(**args). Each connection serves a single
request and is closed by the daemon.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import logging
import os
import signal
from typing import Any

from telegram_mcp._registry import (
    DESTRUCTIVE_TOOLS,
    PEER_SEND_TOOLS,
    WRITE_TOOLS,
    Policy,
    is_read_only_tool,
    load_policy,
    peer_arg,
    text_arg,
)
from telegram_mcp.client import TelegramMCPClient
from telegram_mcp.login import CONFIG_DIR
from telegram_mcp.security import RateLimiter, append_audit

logger = logging.getLogger(__name__)

SOCKET_PATH = os.path.join(CONFIG_DIR, "daemon.sock")
LOCK_PATH = os.path.join(CONFIG_DIR, "daemon.lock")
AUDIT_PATH = os.path.join(CONFIG_DIR, "audit.log")

_ALL_TOOLS_CACHE: frozenset[str] | None = None
_POLICY: Policy | None = None
_WRITE_BUDGET: RateLimiter | None = None
# send_allowlist entries resolved to marked peer ids, and the entries that
# could not be resolved yet (retried on the next refusal).
_RESOLVED_ALLOWLIST: frozenset[int] | None = None
_UNRESOLVED_ALLOWLIST: tuple[int | str, ...] = ()


def _policy() -> Policy:
    """Runtime policy, loaded once from config.json and the environment."""
    global _POLICY
    if _POLICY is None:
        _POLICY = load_policy()
    return _POLICY


def _write_budget() -> RateLimiter:
    global _WRITE_BUDGET
    if _WRITE_BUDGET is None:
        _WRITE_BUDGET = RateLimiter(_policy().write_per_hour, 3600.0)
    return _WRITE_BUDGET


async def _resolve_entries(
    client: TelegramMCPClient, entries: tuple[int | str, ...],
) -> tuple[set[int], tuple[int | str, ...]]:
    resolved: set[int] = set()
    failed: list[int | str] = []
    for entry in entries:
        try:
            resolved.add(await client.resolve_peer_id(entry))
        except Exception as e:
            logger.warning(
                "send_allowlist: entry #%d could not be resolved (%s); it matches nothing",
                entries.index(entry) + 1, type(e).__name__,
            )
            failed.append(entry)
    return resolved, tuple(failed)


async def _resolved_allowlist(client: TelegramMCPClient, refresh: bool = False) -> frozenset[int]:
    """Marked peer ids for the configured send_allowlist.

    Resolved through Telethon on first use (and only then, since it needs a
    connection). An entry that fails to resolve matches nothing; *refresh*
    retries just those entries.
    """
    global _RESOLVED_ALLOWLIST, _UNRESOLVED_ALLOWLIST
    entries = _policy().send_allowlist or ()
    if _RESOLVED_ALLOWLIST is None:
        ids, _UNRESOLVED_ALLOWLIST = await _resolve_entries(client, entries)
        _RESOLVED_ALLOWLIST = frozenset(ids)
    elif refresh and _UNRESOLVED_ALLOWLIST:
        ids, _UNRESOLVED_ALLOWLIST = await _resolve_entries(client, _UNRESOLVED_ALLOWLIST)
        _RESOLVED_ALLOWLIST = _RESOLVED_ALLOWLIST | ids
    return _RESOLVED_ALLOWLIST


async def _peer_allowed(client: TelegramMCPClient, peer: object) -> bool:
    """Whether a send may target *peer* under the active allowlist.

    Both sides are compared as Telethon marked peer ids, resolved the same
    way the send itself will resolve them. Anything that cannot be resolved
    is refused.
    """
    if isinstance(peer, bool) or not isinstance(peer, (int, str)):
        return False
    try:
        peer_id = await client.resolve_peer_id(peer)
    except Exception as e:
        logger.warning("send_allowlist: recipient could not be resolved (%s)", type(e).__name__)
        return False
    if peer_id in await _resolved_allowlist(client):
        return True
    if _UNRESOLVED_ALLOWLIST:
        return peer_id in await _resolved_allowlist(client, refresh=True)
    return False


async def _write_gates(
    client: TelegramMCPClient, tool: str, call_args: dict[str, Any],
) -> str | None:
    """Run the allowlist and budget gates for a write-tier call.

    Returns an error string to refuse with, or None to proceed. Any exception
    is the caller's signal to refuse as well.
    """
    policy = _policy()
    if tool in PEER_SEND_TOOLS and policy.allowlist_active:
        await client.ensure_connected()
        if not await _peer_allowed(client, call_args.get(PEER_SEND_TOOLS[tool])):
            _audit(tool, call_args, "refused:allowlist")
            return f"'{tool}' refused: recipient is not in send_allowlist"

    try:
        _write_budget().acquire()
    except RuntimeError:
        _audit(tool, call_args, "refused:budget")
        return (
            f"'{tool}' refused: hourly write budget of {policy.write_per_hour} calls exhausted"
        )
    return None


def _policy_status() -> dict[str, Any]:
    """Policy summary merged into get_status: counts and flags, never entries."""
    policy = _policy()
    status: dict[str, Any] = {
        "read_only": policy.read_only,
        "write_per_hour": policy.write_per_hour,
        "write_calls_last_hour": _write_budget().used,
    }
    if policy.allowlist_active:
        status["send_allowlist"] = {
            "entries": len(policy.send_allowlist or ()),
            "resolved": len(_RESOLVED_ALLOWLIST) if _RESOLVED_ALLOWLIST is not None else None,
        }
    else:
        status["send_allowlist"] = "off"
    return status


def _all_tools() -> frozenset[str]:
    """Every tool name registered in server.TOOLS.

    Lazy-imported to avoid the circular dependency with server.py (which
    imports SOCKET_PATH from this module).
    """
    global _ALL_TOOLS_CACHE
    if _ALL_TOOLS_CACHE is None:
        from telegram_mcp.server import TOOLS  # noqa: PLC0415 — intentional
        _ALL_TOOLS_CACHE = frozenset(t.name for t in TOOLS)
    return _ALL_TOOLS_CACHE


def _allowed_tools(read_only: bool | None = None) -> frozenset[str]:
    """Tool names this daemon will dispatch.

    In read-only mode (config ``mode: read_only``, ``TELEGRAM_MCP_READ_ONLY``
    in the daemon's environment, or a request tagged ``read_only``) every
    tool that mutates Telegram state or wipes the cache is removed.
    """
    if read_only is None:
        read_only = _policy().read_only
    names = _all_tools()
    if not read_only:
        return names
    return frozenset(n for n in names if is_read_only_tool(n))


def _audit(tool: str, args: dict[str, Any], status: str) -> None:
    peer_key = peer_arg(tool)
    text_key = text_arg(tool)
    try:
        append_audit(
            AUDIT_PATH,
            tool,
            args.get(peer_key) if peer_key else None,
            status,
            args.get(text_key) if text_key else None,
        )
    except Exception:
        logger.exception("Failed to write audit log entry for %s", tool)


class AlreadyRunningError(RuntimeError):
    """Raised when another daemon already holds the lock."""


def _acquire_singleton_lock() -> int:
    """Take an exclusive flock on LOCK_PATH; raise if held by another process.

    Returns the file descriptor; caller keeps it open for the daemon's lifetime
    so the kernel releases the lock on exit (clean or crash).
    """
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            raise AlreadyRunningError("another telegram-mcp daemon is running") from None
        raise
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def _remove_stale_socket() -> None:
    """Best-effort removal of a socket file from a previous run."""
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass


async def _handle_request(client: TelegramMCPClient, payload: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a single {tool, args} payload to the underlying client.

    The safety gates run here, before getattr-based dispatch, so a caller
    speaking the socket protocol directly cannot bypass them:

      1. Whitelist: tool name must appear in the registered TOOLS list.
         Without this, any TelegramMCPClient method would be callable —
         including private ones like _cache_messages (cache poisoning) or
         disconnect (denies service to every concurrent proxy).

      2. Read-only mode: write tools are hidden entirely, either daemon-wide
         (config/env) or for a single request the proxy tagged read_only.

      3. Destructive-tool confirm: requires ``confirm`` to be the JSON
         boolean true. Truthy strings like "yes" or 1 do not count.

      4. Send allowlist: when config.json sets ``send_allowlist``, any tool
         that delivers content to a peer outside it is refused. Recipient
         and allowlist entries are compared as Telethon marked peer ids,
         resolved the same way the send resolves them; anything that fails
         to resolve is refused.

      5. Hourly write budget: a sliding-window cap on write-tier calls, on
         top of the per-second limiter inside the client.

    Gates 4 and 5 fail closed: any exception while evaluating them refuses
    the call. Every write-tier call, including refusals, is appended to the
    audit log.
    """
    req_id = payload.get("id")
    tool = payload.get("tool")
    args = payload.get("args") or {}
    request_read_only = payload.get("read_only") is True

    if not isinstance(tool, str):
        return {"id": req_id, "error": "missing or invalid 'tool'"}
    if not isinstance(args, dict):
        return {"id": req_id, "error": "'args' must be an object"}

    if tool not in _allowed_tools():
        if tool in _all_tools():
            return {"id": req_id, "error": f"'{tool}' is disabled: daemon is in read-only mode"}
        return {"id": req_id, "error": f"unknown tool: {tool}"}
    if request_read_only and not is_read_only_tool(tool):
        return {"id": req_id, "error": f"'{tool}' is disabled: session is in read-only mode"}

    if tool in DESTRUCTIVE_TOOLS and args.get("confirm") is not True:
        return {
            "id": req_id,
            "result": {
                "warning": (
                    f"'{tool}' is a destructive action. "
                    "Call again with confirm=true to proceed."
                ),
                "would_do": f"Execute {tool} (provide confirm=true to proceed)",
            },
        }

    method = getattr(client, tool, None)
    if method is None or not callable(method):
        return {"id": req_id, "error": f"unknown tool: {tool}"}

    call_args = {k: v for k, v in args.items() if k != "confirm"}
    is_write = tool in WRITE_TOOLS

    if is_write:
        try:
            refusal = await _write_gates(client, tool, call_args)
        except Exception as e:
            # Fail closed: a broken gate must never let a write through.
            logger.exception("Policy gate failed for %s; refusing", tool)
            _audit(tool, call_args, "refused:gate_error")
            return {"id": req_id, "error": f"'{tool}' refused: policy check failed ({e})"}
        if refusal:
            return {"id": req_id, "error": refusal}

    try:
        await client.ensure_connected()
        result = await method(**call_args)
    except TypeError as e:
        if is_write:
            _audit(tool, call_args, "error:bad_args")
        return {"id": req_id, "error": f"bad args for {tool}: {e}"}
    except Exception as e:
        logger.exception("Tool %s failed", tool)
        if is_write:
            _audit(tool, call_args, "error")
        return {"id": req_id, "error": str(e)}

    if is_write:
        _audit(tool, call_args, "ok")
    if tool == "get_status" and isinstance(result, dict):
        result = {**result, **_policy_status()}
    return {"id": req_id, "result": result}


def _make_handler(client: TelegramMCPClient):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                payload = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                response = {"id": None, "error": f"invalid JSON: {e}"}
            else:
                response = await _handle_request(client, payload)
            writer.write((json.dumps(response, default=str) + "\n").encode("utf-8"))
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logger.exception("Unhandled error in daemon handler")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    return handle


async def serve_daemon() -> None:
    """Run the daemon: open Telethon session, listen on the Unix socket."""
    lock_fd = _acquire_singleton_lock()
    _remove_stale_socket()

    client = TelegramMCPClient()
    await client.connect()

    # Create the socket with a restrictive umask so it is never world-accessible,
    # even briefly, between creation and the chmod below (closes a local-access race).
    old_umask = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(_make_handler(client), path=SOCKET_PATH)
    finally:
        os.umask(old_umask)
    os.chmod(SOCKET_PATH, 0o600)
    policy = _policy()
    logger.info(
        "telegram-mcp daemon listening on %s (pid=%d, read_only=%s, send_allowlist=%s,"
        " write_per_hour=%d)",
        SOCKET_PATH, os.getpid(), policy.read_only,
        "off" if policy.send_allowlist is None else f"{len(policy.send_allowlist)} peers",
        policy.write_per_hour,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    try:
        async with server:
            serve_task = asyncio.create_task(server.serve_forever())
            await stop.wait()
            serve_task.cancel()
            try:
                await serve_task
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        await client.disconnect()
        _remove_stale_socket()
        try:
            os.unlink(LOCK_PATH)
        except FileNotFoundError:
            pass
        os.close(lock_fd)
