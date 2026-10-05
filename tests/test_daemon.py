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


@pytest.fixture
def policy(monkeypatch, tmp_path):
    """Install a Policy on the daemon module and point the audit log at tmp.

    Returns a setter so tests can swap the policy mid-test; the write budget
    is reset whenever the policy changes.
    """
    from telegram_mcp._registry import Policy

    monkeypatch.setattr(daemon, "AUDIT_PATH", str(tmp_path / "audit.log"))

    def set_policy(**kwargs):
        monkeypatch.setattr(daemon, "_POLICY", Policy(**kwargs))
        monkeypatch.setattr(daemon, "_WRITE_BUDGET", None)
        monkeypatch.setattr(daemon, "_RESOLVED_ALLOWLIST", None)
        monkeypatch.setattr(daemon, "_UNRESOLVED_ALLOWLIST", ())

    set_policy()
    return set_policy


def _client_with(**methods):
    client = MagicMock()
    client.ensure_connected = AsyncMock()
    for name, value in methods.items():
        setattr(client, name, value)
    return client


class TestConfirmMustBeJsonTrue:
    """The gate used to be truthy: "yes", 1 and "false" all passed it."""

    @pytest.mark.parametrize("confirm", ["true", "yes", 1, "false", [True], {"ok": 1}])
    async def test_truthy_non_bool_confirm_is_refused(self, policy, confirm):
        client = _client_with(delete_chat=AsyncMock())
        result = await daemon._handle_request(
            client, {"id": 1, "tool": "delete_chat", "args": {"chat_id": 1, "confirm": confirm}}
        )
        assert "warning" in result["result"], f"confirm={confirm!r} slipped through"
        client.delete_chat.assert_not_awaited()

    @pytest.mark.parametrize(
        "tool,args",
        [
            ("get_invite_link", {"chat_id": 1}),
            ("add_participant", {"chat_id": 1, "user_id": 2}),
            ("create_channel", {"title": "t"}),
            ("create_group", {"title": "t", "users": [1]}),
            ("set_chat_title", {"chat_id": 1, "title": "t"}),
            ("set_chat_description", {"chat_id": 1, "description": "d"}),
            ("set_chat_photo", {"chat_id": 1, "file_path": "/tmp/x"}),
        ],
    )
    async def test_newly_gated_tools_need_confirm(self, policy, tool, args):
        method = AsyncMock(return_value={"status": "ok"})
        client = _client_with(**{tool: method})
        result = await daemon._handle_request(client, {"id": 1, "tool": tool, "args": args})
        assert "warning" in result["result"], f"{tool} is not gated"
        method.assert_not_awaited()

        result = await daemon._handle_request(
            client, {"id": 2, "tool": tool, "args": {**args, "confirm": True}}
        )
        assert result["result"] == {"status": "ok"}
        method.assert_awaited_once_with(**args)


class TestReadOnlyMode:
    async def test_daemon_policy_hides_write_tools(self, policy):
        policy(read_only=True)
        client = _client_with(send_message=AsyncMock(), list_chats=AsyncMock(return_value=[]))

        result = await daemon._handle_request(
            client, {"id": 1, "tool": "send_message", "args": {"chat_id": 1, "text": "hi"}}
        )
        assert "read-only" in result["error"]
        client.send_message.assert_not_awaited()

        result = await daemon._handle_request(client, {"id": 2, "tool": "list_chats", "args": {}})
        assert result["result"] == []

    async def test_clear_cache_hidden_in_read_only(self, policy):
        policy(read_only=True)
        client = _client_with(clear_cache=AsyncMock())
        result = await daemon._handle_request(client, {"id": 1, "tool": "clear_cache", "args": {}})
        assert "read-only" in result["error"]
        client.clear_cache.assert_not_awaited()

    async def test_request_flag_hides_write_tools_even_if_daemon_is_not_read_only(self, policy):
        client = _client_with(send_message=AsyncMock(), get_me=AsyncMock(return_value={"id": 1}))

        result = await daemon._handle_request(
            client,
            {"id": 1, "tool": "send_message", "args": {"chat_id": 1, "text": "hi"},
             "read_only": True},
        )
        assert "read-only" in result["error"]
        client.send_message.assert_not_awaited()

        # A tagged read is fine; a non-boolean tag does not enable read-only
        result = await daemon._handle_request(
            client, {"id": 2, "tool": "get_me", "args": {}, "read_only": True}
        )
        assert result["result"] == {"id": 1}

    async def test_allowed_tools_sets(self, policy):
        from telegram_mcp._registry import WRITE_TOOLS

        full = daemon._allowed_tools()
        ro = daemon._allowed_tools(read_only=True)
        assert WRITE_TOOLS <= full
        assert not (WRITE_TOOLS & ro)
        assert "clear_cache" not in ro
        assert {"list_chats", "read_messages", "search_regex", "download_media",
                "export_chat", "sync_messages", "get_status"} <= ro

    async def test_env_var_read_in_policy(self, monkeypatch):
        from telegram_mcp._registry import policy_from_config

        assert policy_from_config({}, {"TELEGRAM_MCP_READ_ONLY": "1"}).read_only is True
        assert policy_from_config({}, {"TELEGRAM_MCP_READ_ONLY": "0"}).read_only is False
        assert policy_from_config({"mode": "read_only"}, {}).read_only is True
        assert policy_from_config({"mode": "full"}, {}).read_only is False


# Marked peer ids the fake resolver knows about. Mirrors what Telethon's
# get_input_entity + get_peer_id would return: every spelling of a peer maps
# to one id; a bare id can belong to a user or a channel.
_PEERS = {
    42: 42, "42": 42,
    "@alice": 777, "alice": 777, "@ALICE": 777, "ALICE": 777,
    "https://t.me/alice": 777, "t.me/alice": 777, "+34600000000": 777,
    "@bob": 888, "bob": 888,
    "me": 1, "self": 1, "@me": 1,
    # bare id 5 is a *channel* here; -1000000000005 is its marked id
    5: -1000000000005, -1000000000005: -1000000000005,
}


def _fake_resolver(peer):
    try:
        return _PEERS[peer]
    except KeyError:
        raise ValueError("Cannot find any entity corresponding to the peer") from None


def _resolving_client(**methods):
    client = _client_with(**methods)
    client.resolve_peer_id = AsyncMock(side_effect=_fake_resolver)
    return client


class TestSendAllowlist:
    @pytest.mark.parametrize(
        "tool,args",
        [
            ("send_message", {"chat_id": 999, "text": "hi"}),
            ("send_file", {"chat_id": "@stranger", "file_path": "/tmp/x"}),
            ("send_voice", {"chat_id": 999, "file_path": "/tmp/x"}),
            ("send_location", {"chat_id": 999, "lat": 1.0, "lon": 2.0}),
            ("schedule_message", {"chat_id": 999, "text": "hi", "schedule_date": "2030-01-01"}),
            ("edit_message", {"chat_id": "@bob", "message_id": 1, "text": "hi"}),
            ("send_reaction", {"chat_id": "@bob", "message_id": 1, "emoji": "x"}),
            ("forward_message", {"from_chat": 42, "message_ids": [1], "to_chat": 999,
                                 "confirm": True}),
        ],
    )
    async def test_peer_outside_allowlist_is_refused(self, policy, tool, args):
        policy(send_allowlist=(42, "@alice"))
        method = AsyncMock()
        client = _resolving_client(**{tool: method})
        result = await daemon._handle_request(client, {"id": 1, "tool": tool, "args": args})
        assert "send_allowlist" in result["error"], f"{tool} not enforced"
        method.assert_not_awaited()

    async def test_every_spelling_of_an_allowed_peer_passes(self, policy):
        """Comparison is by resolved peer id, so any spelling Telethon accepts works."""
        policy(send_allowlist=(42, "@alice"))
        client = _resolving_client(send_message=AsyncMock(return_value={"id": 1}))
        for peer in (42, "42", "@alice", "alice", "@ALICE", "https://t.me/alice",
                     "+34600000000"):
            result = await daemon._handle_request(
                client, {"id": 1, "tool": "send_message", "args": {"chat_id": peer, "text": "x"}}
            )
            assert "error" not in result, f"{peer!r} wrongly refused: {result}"

    async def test_allowlist_entry_spelling_does_not_matter_either(self, policy):
        policy(send_allowlist=("t.me/alice",))
        client = _resolving_client(send_message=AsyncMock(return_value={"id": 1}))
        result = await daemon._handle_request(
            client, {"id": 1, "tool": "send_message", "args": {"chat_id": "@ALICE", "text": "x"}}
        )
        assert "error" not in result

    async def test_bare_id_namespace_is_not_trusted(self, policy):
        """Allowlisting user id 5 must not admit channel 5 (marked -100...5), and
        vice versa: both sides go through the same resolution."""
        policy(send_allowlist=(5,))  # resolves to the channel
        client = _resolving_client(send_message=AsyncMock(return_value={"id": 1}))
        ok = await daemon._handle_request(
            client, {"id": 1, "tool": "send_message",
                     "args": {"chat_id": -1000000000005, "text": "x"}}
        )
        assert "error" not in ok
        # A peer that merely *looks* like 5 as a string but resolves elsewhere is refused
        client.resolve_peer_id = AsyncMock(side_effect=lambda p: 5 if p == 5 else 999)
        policy(send_allowlist=(5,))
        refused = await daemon._handle_request(
            client, {"id": 2, "tool": "send_message", "args": {"chat_id": "@five", "text": "x"}}
        )
        assert "send_allowlist" in refused["error"]

    async def test_unresolvable_recipient_is_refused(self, policy):
        policy(send_allowlist=(42,))
        client = _resolving_client(send_message=AsyncMock(return_value={"id": 1}))
        for peer in ("@nobody", 3.5, None, True, [42], {"id": 42}):
            result = await daemon._handle_request(
                client, {"id": 1, "tool": "send_message", "args": {"chat_id": peer, "text": "x"}}
            )
            assert "send_allowlist" in result.get("error", ""), f"{peer!r} slipped through"
        client.send_message.assert_not_awaited()

    async def test_unresolvable_entry_matches_nothing_and_is_retried(self, policy):
        policy(send_allowlist=("@alice", "@notyet"))
        client = _resolving_client(send_message=AsyncMock(return_value={"id": 1}))

        refused = await daemon._handle_request(
            client, {"id": 1, "tool": "send_message", "args": {"chat_id": "@bob", "text": "x"}}
        )
        assert "send_allowlist" in refused["error"]
        assert daemon._UNRESOLVED_ALLOWLIST == ("@notyet",)

        # The entry becomes resolvable (e.g. the user is now in contacts): the
        # next refusal re-resolves it and the send goes through.
        _PEERS["@notyet"] = 888
        try:
            ok = await daemon._handle_request(
                client, {"id": 2, "tool": "send_message", "args": {"chat_id": "@bob", "text": "x"}}
            )
        finally:
            del _PEERS["@notyet"]
        assert "error" not in ok
        assert daemon._UNRESOLVED_ALLOWLIST == ()

    async def test_resolution_uses_the_connected_client(self, policy):
        policy(send_allowlist=(42,))
        client = _resolving_client(send_message=AsyncMock(return_value={"id": 1}))
        await daemon._handle_request(
            client, {"id": 1, "tool": "send_message", "args": {"chat_id": 42, "text": "x"}}
        )
        assert client.ensure_connected.await_count >= 1
        resolved_with = [c.args[0] for c in client.resolve_peer_id.await_args_list]
        assert resolved_with == [42, 42]  # recipient, then the one allowlist entry

    async def test_gate_exception_fails_closed(self, policy):
        """A broken gate (resolver blowing up, connection failure, policy bug)
        must refuse the write, not fall through to dispatch."""
        policy(send_allowlist=(42,))
        client = _resolving_client(send_message=AsyncMock(return_value={"id": 1}))
        client.ensure_connected = AsyncMock(side_effect=ConnectionError("down"))
        result = await daemon._handle_request(
            client, {"id": 1, "tool": "send_message", "args": {"chat_id": 42, "text": "x"}}
        )
        assert "policy check failed" in result["error"]
        client.send_message.assert_not_awaited()

        client = _resolving_client(mark_read=AsyncMock(return_value={}))
        monkey_budget = MagicMock()
        monkey_budget.acquire.side_effect = ZeroDivisionError("bug")
        daemon._WRITE_BUDGET = monkey_budget
        result = await daemon._handle_request(
            client, {"id": 2, "tool": "mark_read", "args": {"chat_id": 1}}
        )
        assert "policy check failed" in result["error"]
        client.mark_read.assert_not_awaited()

    async def test_forward_source_is_not_checked_only_destination(self, policy):
        policy(send_allowlist=(42,))
        client = _resolving_client(forward_message=AsyncMock(return_value={"status": "forwarded"}))
        result = await daemon._handle_request(
            client,
            {"id": 1, "tool": "forward_message",
             "args": {"from_chat": 999, "message_ids": [1], "to_chat": 42, "confirm": True}},
        )
        assert result["result"] == {"status": "forwarded"}

    async def test_forward_without_confirm_never_reaches_allowlist(self, policy):
        policy(send_allowlist=(42,))
        client = _resolving_client(forward_message=AsyncMock())
        result = await daemon._handle_request(
            client,
            {"id": 1, "tool": "forward_message",
             "args": {"from_chat": 1, "message_ids": [1], "to_chat": 999}},
        )
        assert "warning" in result["result"]
        client.resolve_peer_id.assert_not_awaited()
        client.forward_message.assert_not_awaited()

    async def test_reads_unaffected(self, policy):
        policy(send_allowlist=(42,))
        client = _resolving_client(read_messages=AsyncMock(return_value=[]))
        result = await daemon._handle_request(
            client, {"id": 1, "tool": "read_messages", "args": {"chat_id": 999}}
        )
        assert result["result"] == []
        client.resolve_peer_id.assert_not_awaited()

    async def test_no_allowlist_means_no_resolution(self, policy):
        policy()
        client = _resolving_client(send_message=AsyncMock(return_value={"id": 1}))
        result = await daemon._handle_request(
            client, {"id": 1, "tool": "send_message", "args": {"chat_id": "@anyone", "text": "x"}}
        )
        assert "error" not in result
        client.resolve_peer_id.assert_not_awaited()

    async def test_allowlist_not_settable_by_tool(self):
        from telegram_mcp.server import TOOLS

        names = {t.name for t in TOOLS}
        assert not any("allowlist" in n or "policy" in n or "config" in n for n in names)
        for t in TOOLS:
            assert "send_allowlist" not in t.inputSchema.get("properties", {})


class TestStatusReportsPolicy:
    async def test_get_status_carries_policy_summary_not_entries(self, policy):
        policy(read_only=False, send_allowlist=(42, "@alice"), write_per_hour=7)
        client = _resolving_client(
            get_status=AsyncMock(return_value={"connected": True}),
            mark_read=AsyncMock(return_value={}),
        )
        await daemon._handle_request(client, {"id": 0, "tool": "mark_read", "args": {"chat_id": 1}})
        result = await daemon._handle_request(client, {"id": 1, "tool": "get_status", "args": {}})
        body = result["result"]
        assert body["connected"] is True
        assert body["read_only"] is False
        assert body["write_per_hour"] == 7
        assert body["write_calls_last_hour"] == 1
        assert body["send_allowlist"] == {"entries": 2, "resolved": None}
        assert "alice" not in str(body) and "42" not in str(body["send_allowlist"])

    async def test_get_status_shows_read_only_daemon(self, policy):
        policy(read_only=True)
        client = _client_with(get_status=AsyncMock(return_value={"connected": True}))
        result = await daemon._handle_request(client, {"id": 1, "tool": "get_status", "args": {}})
        assert result["result"]["read_only"] is True
        assert result["result"]["send_allowlist"] == "off"


class TestWriteBudgetAndAudit:
    async def test_hourly_budget_refuses_after_cap(self, policy):
        policy(write_per_hour=2)
        client = _client_with(mark_read=AsyncMock(return_value={"status": "marked_read"}))
        for i in range(2):
            result = await daemon._handle_request(
                client, {"id": i, "tool": "mark_read", "args": {"chat_id": 1}}
            )
            assert "error" not in result
        result = await daemon._handle_request(
            client, {"id": 3, "tool": "mark_read", "args": {"chat_id": 1}}
        )
        assert "write budget" in result["error"]
        assert client.mark_read.await_count == 2

    async def test_budget_does_not_count_reads(self, policy):
        policy(write_per_hour=1)
        client = _client_with(
            list_chats=AsyncMock(return_value=[]),
            mark_read=AsyncMock(return_value={"status": "ok"}),
        )
        for i in range(5):
            await daemon._handle_request(client, {"id": i, "tool": "list_chats", "args": {}})
        result = await daemon._handle_request(
            client, {"id": 9, "tool": "mark_read", "args": {"chat_id": 1}}
        )
        assert "error" not in result

    async def test_audit_log_records_write_calls(self, policy, tmp_path):
        client = _client_with(
            send_message=AsyncMock(return_value={"id": 1}),
            list_chats=AsyncMock(return_value=[]),
        )
        text = "hello " * 30
        await daemon._handle_request(
            client, {"id": 1, "tool": "send_message", "args": {"chat_id": 777, "text": text}}
        )
        await daemon._handle_request(client, {"id": 2, "tool": "list_chats", "args": {}})

        audit = tmp_path / "audit.log"
        assert oct(audit.stat().st_mode & 0o777) == "0o600"
        lines = audit.read_text().splitlines()
        assert len(lines) == 1, lines
        assert "tool=send_message" in lines[0]
        assert "peer=777" in lines[0]
        assert "status=ok" in lines[0]
        assert text[:80] in lines[0]
        assert text[:81] not in lines[0]

    async def test_audit_log_records_refusals_and_errors(self, policy, tmp_path):
        policy(send_allowlist=(1,), write_per_hour=1)
        client = _client_with(
            send_message=AsyncMock(side_effect=RuntimeError("flood")),
            mark_read=AsyncMock(return_value={}),
        )
        client.resolve_peer_id = AsyncMock(side_effect=lambda p: p)
        await daemon._handle_request(
            client, {"id": 1, "tool": "send_message", "args": {"chat_id": 2, "text": "x"}}
        )
        await daemon._handle_request(
            client, {"id": 2, "tool": "send_message", "args": {"chat_id": 1, "text": "x"}}
        )
        await daemon._handle_request(client, {"id": 3, "tool": "mark_read", "args": {"chat_id": 1}})
        lines = (tmp_path / "audit.log").read_text().splitlines()
        statuses = [line.split("status=")[1].split(" ")[0] for line in lines]
        assert statuses == ["refused:allowlist", "error", "refused:budget"]
