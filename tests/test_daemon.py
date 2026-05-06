"""Tests for the daemon protocol, singleton lock, and request dispatch."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_mcp import daemon


@pytest.fixture
def tmp_lock_path(monkeypatch, tmp_path):
    """Redirect daemon LOCK_PATH and SOCKET_PATH into a temp dir."""
    monkeypatch.setattr(daemon, "LOCK_PATH", str(tmp_path / "daemon.lock"))
    monkeypatch.setattr(daemon, "SOCKET_PATH", str(tmp_path / "daemon.sock"))
    monkeypatch.setattr(daemon, "CONFIG_DIR", str(tmp_path))
    return tmp_path


class TestSingletonLock:
    def test_first_acquire_succeeds(self, tmp_lock_path):
        fd = daemon._acquire_singleton_lock()
        try:
            assert os.path.exists(daemon.LOCK_PATH)
        finally:
            os.close(fd)

    def test_second_acquire_in_subprocess_fails(self, tmp_lock_path):
        """A second daemon process must hit AlreadyRunningError.

        flock locks are per-process, so we need a real subprocess to test the
        contention case. We fork() so the child inherits the same lock file
        path setup but gets its own file descriptor and process for flock to
        treat as foreign.
        """
        fd = daemon._acquire_singleton_lock()
        try:
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(r)
                try:
                    daemon._acquire_singleton_lock()
                    os.write(w, b"unexpectedly_acquired")
                except daemon.AlreadyRunningError:
                    os.write(w, b"already_running")
                except Exception as e:
                    os.write(w, f"other_error:{e}".encode())
                finally:
                    os.close(w)
                    os._exit(0)
            os.close(w)
            os.waitpid(pid, 0)
            result = os.read(r, 1024).decode()
            os.close(r)
            assert result == "already_running"
        finally:
            os.close(fd)


class TestHandleRequest:
    async def test_unknown_tool_returns_error(self):
        client = MagicMock()
        client.ensure_connected = AsyncMock()
        # No method called "nonexistent" on MagicMock by default — but
        # MagicMock returns auto-generated attrs, so we explicitly delete
        # and use spec to prevent that.
        client = MagicMock(spec=[])
        client.ensure_connected = AsyncMock()
        result = await daemon._handle_request(
            client, {"id": 7, "tool": "nonexistent", "args": {}}
        )
        assert result["id"] == 7
        assert "unknown tool" in result["error"]

    async def test_missing_tool_returns_error(self):
        client = MagicMock(spec=[])
        result = await daemon._handle_request(client, {"id": 1, "args": {}})
        assert result["id"] == 1
        assert "tool" in result["error"]

    async def test_bad_args_type_returns_error(self):
        client = MagicMock(spec=[])
        result = await daemon._handle_request(
            client, {"id": 2, "tool": "list_chats", "args": "not-a-dict"}
        )
        assert result["id"] == 2
        assert "object" in result["error"]

    async def test_successful_dispatch(self):
        client = MagicMock()
        client.ensure_connected = AsyncMock()
        client.list_chats = AsyncMock(return_value=[{"id": 1, "name": "Chat"}])
        result = await daemon._handle_request(
            client, {"id": 42, "tool": "list_chats", "args": {"limit": 10}}
        )
        assert result == {"id": 42, "result": [{"id": 1, "name": "Chat"}]}
        client.list_chats.assert_awaited_once_with(limit=10)

    async def test_method_exception_becomes_error(self):
        client = MagicMock()
        client.ensure_connected = AsyncMock()
        client.list_chats = AsyncMock(side_effect=ValueError("boom"))
        result = await daemon._handle_request(
            client, {"id": 99, "tool": "list_chats", "args": {}}
        )
        assert result["id"] == 99
        assert result["error"] == "boom"

    async def test_typeerror_for_bad_args_signature(self):
        client = MagicMock()
        client.ensure_connected = AsyncMock()

        async def takes_no_args():
            return None

        # list_chats is in the whitelist; the bad kwarg makes it raise TypeError
        client.list_chats = takes_no_args
        result = await daemon._handle_request(
            client, {"id": 5, "tool": "list_chats", "args": {"wrong_kwarg": 1}}
        )
        assert result["id"] == 5
        assert "bad args" in result["error"]


class TestWhitelist:
    """The whitelist must reject any tool name not registered in TOOLS,
    even if it would resolve via getattr on the real client."""

    async def test_private_method_rejected(self):
        client = MagicMock()
        client.ensure_connected = AsyncMock()
        client._cache_messages = MagicMock()
        client.disconnect = AsyncMock()
        client.connect = AsyncMock()
        client._start_listener = MagicMock()

        for private in ("_cache_messages", "disconnect", "connect", "_start_listener"):
            result = await daemon._handle_request(
                client, {"id": 1, "tool": private, "args": {}}
            )
            assert "unknown tool" in result["error"], f"{private} should be rejected"

        # None of the lifecycle methods were ever invoked
        client._cache_messages.assert_not_called()
        client.disconnect.assert_not_awaited()
        client.connect.assert_not_awaited()
        client._start_listener.assert_not_called()

    async def test_dunder_methods_rejected(self):
        client = MagicMock()
        client.ensure_connected = AsyncMock()
        result = await daemon._handle_request(
            client, {"id": 1, "tool": "__class__", "args": {}}
        )
        assert "unknown tool" in result["error"]


class TestDestructiveGate:
    async def test_destructive_without_confirm_returns_warning(self):
        client = MagicMock()
        client.ensure_connected = AsyncMock()
        client.delete_chat = AsyncMock()

        result = await daemon._handle_request(
            client, {"id": 5, "tool": "delete_chat", "args": {"chat_id": 123}}
        )
        # Warning is returned as a successful result, not an error
        assert "error" not in result
        assert "warning" in result["result"]
        assert "destructive" in result["result"]["warning"].lower()
        client.delete_chat.assert_not_awaited()

    async def test_destructive_with_confirm_strips_and_dispatches(self):
        client = MagicMock()
        client.ensure_connected = AsyncMock()
        client.delete_chat = AsyncMock(return_value={"status": "deleted"})

        result = await daemon._handle_request(
            client,
            {"id": 6, "tool": "delete_chat", "args": {"chat_id": 123, "confirm": True}},
        )
        assert result["result"] == {"status": "deleted"}
        client.delete_chat.assert_awaited_once_with(chat_id=123)

    async def test_non_destructive_passes_confirm_through_stripped(self):
        """A confirm flag on a non-destructive tool should be stripped silently
        so it never reaches the underlying method (which doesn't accept it)."""
        client = MagicMock()
        client.ensure_connected = AsyncMock()
        client.list_chats = AsyncMock(return_value=[])

        result = await daemon._handle_request(
            client,
            {"id": 7, "tool": "list_chats", "args": {"limit": 5, "confirm": True}},
        )
        assert result["result"] == []
        client.list_chats.assert_awaited_once_with(limit=5)


@pytest.fixture
def short_sock_path(tmp_path_factory):
    """A Unix socket path short enough for macOS's 104-char AF_UNIX limit."""
    import tempfile
    import uuid

    path = os.path.join(tempfile.gettempdir(), f"tg-mcp-test-{uuid.uuid4().hex[:8]}.sock")
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


class TestSocketProtocol:
    async def test_round_trip_via_socket(self, short_sock_path):
        """Spin up the handler over a real Unix socket, send a request, parse the response."""
        client = MagicMock()
        client.ensure_connected = AsyncMock()
        client.get_me = AsyncMock(return_value={"id": 1, "name": "Test"})

        handler = daemon._make_handler(client)
        server = await asyncio.start_unix_server(handler, path=short_sock_path)

        try:
            reader, writer = await asyncio.open_unix_connection(short_sock_path)
            writer.write(json.dumps({"id": 1, "tool": "get_me", "args": {}}).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.close()
            await writer.wait_closed()

            response = json.loads(line.decode())
            assert response == {"id": 1, "result": {"id": 1, "name": "Test"}}
        finally:
            server.close()
            await server.wait_closed()

    async def test_invalid_json_returns_error(self, short_sock_path):
        client = MagicMock(spec=[])
        handler = daemon._make_handler(client)
        server = await asyncio.start_unix_server(handler, path=short_sock_path)
        try:
            reader, writer = await asyncio.open_unix_connection(short_sock_path)
            writer.write(b"not valid json\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.close()
            await writer.wait_closed()
            response = json.loads(line.decode())
            assert "invalid JSON" in response["error"]
        finally:
            server.close()
            await server.wait_closed()
