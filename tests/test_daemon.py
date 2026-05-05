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

        async def takes_one_arg(x):
            return x

        client.something = takes_one_arg
        result = await daemon._handle_request(
            client, {"id": 5, "tool": "something", "args": {"wrong_kwarg": 1}}
        )
        assert result["id"] == 5
        assert "bad args" in result["error"]


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
