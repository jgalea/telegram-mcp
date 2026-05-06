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

from telegram_mcp._registry import DESTRUCTIVE_TOOLS
from telegram_mcp.client import TelegramMCPClient
from telegram_mcp.login import CONFIG_DIR

logger = logging.getLogger(__name__)

SOCKET_PATH = os.path.join(CONFIG_DIR, "daemon.sock")
LOCK_PATH = os.path.join(CONFIG_DIR, "daemon.lock")

_ALLOWED_TOOLS_CACHE: frozenset[str] | None = None


def _allowed_tools() -> frozenset[str]:
    """Return the set of tool names exposed by the MCP.

    Lazy-imported to avoid the circular dependency with server.py (which
    imports SOCKET_PATH from this module). Caching keeps the per-request
    cost negligible.
    """
    global _ALLOWED_TOOLS_CACHE
    if _ALLOWED_TOOLS_CACHE is None:
        from telegram_mcp.server import TOOLS  # noqa: PLC0415 — intentional
        _ALLOWED_TOOLS_CACHE = frozenset(t.name for t in TOOLS)
    return _ALLOWED_TOOLS_CACHE


class AlreadyRunningError(RuntimeError):
    """Raised when another daemon already holds the lock."""


def _acquire_singleton_lock() -> int:
    """Take an exclusive flock on LOCK_PATH; raise if held by another process.

    Returns the file descriptor; caller keeps it open for the daemon's lifetime
    so the kernel releases the lock on exit (clean or crash).
    """
    os.makedirs(CONFIG_DIR, exist_ok=True)
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

    Two safety gates run before getattr-based dispatch:

      1. Whitelist: tool name must appear in the registered TOOLS list.
         Without this, any TelegramMCPClient method would be callable —
         including private ones like _cache_messages (cache poisoning) or
         disconnect (denies service to every concurrent proxy).

      2. Destructive-tool confirm: tools that mutate or destroy state
         require an explicit confirm=True flag. Lives here (not just in
         the proxy) so a caller speaking the socket protocol directly
         cannot bypass the safety gate.
    """
    req_id = payload.get("id")
    tool = payload.get("tool")
    args = payload.get("args") or {}

    if not isinstance(tool, str):
        return {"id": req_id, "error": "missing or invalid 'tool'"}
    if not isinstance(args, dict):
        return {"id": req_id, "error": "'args' must be an object"}

    if tool not in _allowed_tools():
        return {"id": req_id, "error": f"unknown tool: {tool}"}

    if tool in DESTRUCTIVE_TOOLS and not args.get("confirm"):
        return {
            "id": req_id,
            "result": {
                "warning": (
                    f"'{tool}' is a destructive action. "
                    "Call again with confirm=true to proceed."
                ),
                "would_do": f"Execute {tool} with args: {args}",
            },
        }

    method = getattr(client, tool, None)
    if method is None or not callable(method):
        return {"id": req_id, "error": f"unknown tool: {tool}"}

    call_args = {k: v for k, v in args.items() if k != "confirm"}

    try:
        await client.ensure_connected()
        result = await method(**call_args)
        return {"id": req_id, "result": result}
    except TypeError as e:
        return {"id": req_id, "error": f"bad args for {tool}: {e}"}
    except Exception as e:
        logger.exception("Tool %s failed", tool)
        return {"id": req_id, "error": str(e)}


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

    server = await asyncio.start_unix_server(_make_handler(client), path=SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o600)
    logger.info("telegram-mcp daemon listening on %s (pid=%d)", SOCKET_PATH, os.getpid())

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
