"""Test that all tools are registered and well-formed."""

import asyncio

import pytest

from telegram_mcp import server as server_module
from telegram_mcp._registry import DESTRUCTIVE_TOOLS
from telegram_mcp.server import TOOLS, call_tool


class TestToolRegistration:
    def test_tool_count(self):
        assert len(TOOLS) == 54

    def test_all_tools_have_names(self):
        for tool in TOOLS:
            assert tool.name, f"Tool missing name: {tool}"

    def test_all_tools_have_descriptions(self):
        for tool in TOOLS:
            assert tool.description, f"Tool {tool.name} missing description"

    def test_all_tools_have_schemas(self):
        for tool in TOOLS:
            assert tool.inputSchema is not None, f"Tool {tool.name} missing schema"
            assert tool.inputSchema.get("type") == "object"

    def test_destructive_tools_have_confirm(self):
        for tool in TOOLS:
            if tool.name in DESTRUCTIVE_TOOLS:
                props = tool.inputSchema.get("properties", {})
                assert "confirm" in props, f"Destructive tool {tool.name} missing confirm param"

    def test_no_duplicate_names(self):
        names = [t.name for t in TOOLS]
        assert len(names) == len(set(names)), (
            f"Duplicate tool names: {[n for n in names if names.count(n) > 1]}"
        )

    def test_destructive_tool_descriptions(self):
        for tool in TOOLS:
            if tool.name in DESTRUCTIVE_TOOLS:
                assert "DESTRUCTIVE" in tool.description, (
                    f"{tool.name} should mention DESTRUCTIVE"
                )

    def test_get_new_messages_tool_exists(self):
        names = [t.name for t in TOOLS]
        assert "get_new_messages" in names

    def test_get_new_messages_requires_since(self):
        tool = next(t for t in TOOLS if t.name == "get_new_messages")
        assert "since" in tool.inputSchema.get("required", [])

    def test_search_messages_has_chat_type(self):
        tool = next(t for t in TOOLS if t.name == "search_messages")
        props = tool.inputSchema.get("properties", {})
        assert "chat_type" in props
        assert props["chat_type"]["enum"] == ["user", "group", "channel"]

    def test_send_message_has_parse_mode(self):
        tool = next(t for t in TOOLS if t.name == "send_message")
        props = tool.inputSchema.get("properties", {})
        assert "parse_mode" in props
        assert props["parse_mode"]["enum"] == ["md", "html"]

    def test_edit_message_has_parse_mode(self):
        tool = next(t for t in TOOLS if t.name == "edit_message")
        props = tool.inputSchema.get("properties", {})
        assert "parse_mode" in props
        assert props["parse_mode"]["enum"] == ["md", "html"]

    def test_sync_messages_tool_exists(self):
        names = [t.name for t in TOOLS]
        assert "sync_messages" in names

    def test_sync_messages_schema(self):
        tool = next(t for t in TOOLS if t.name == "sync_messages")
        props = tool.inputSchema.get("properties", {})
        assert "chat_id" in props
        assert "limit" in props
        assert "max_chats" in props

    def test_search_regex_tool_exists(self):
        names = [t.name for t in TOOLS]
        assert "search_regex" in names

    def test_search_regex_requires_pattern(self):
        tool = next(t for t in TOOLS if t.name == "search_regex")
        assert "pattern" in tool.inputSchema.get("required", [])

    def test_chat_analytics_tool_exists(self):
        names = [t.name for t in TOOLS]
        assert "chat_analytics" in names

    def test_message_timeline_tool_exists(self):
        names = [t.name for t in TOOLS]
        assert "message_timeline" in names

    def test_today_messages_tool_exists(self):
        names = [t.name for t in TOOLS]
        assert "today_messages" in names

    def test_export_cached_messages_tool_exists(self):
        names = [t.name for t in TOOLS]
        assert "export_cached_messages" in names

    def test_download_chat_media_tool_exists(self):
        names = [t.name for t in TOOLS]
        assert "download_chat_media" in names

    def test_download_chat_media_requires_chat_id(self):
        tool = next(t for t in TOOLS if t.name == "download_chat_media")
        assert "chat_id" in tool.inputSchema.get("required", [])


class TestCallToolErrorPropagation:
    """call_tool must raise, not swallow, so the MCP SDK can flag isError=true.

    If we swallow the exception and return a TextContent with an `error` field,
    the SDK treats it as a successful tool call. Some Claude sessions then
    misread or hallucinate around the error instead of surfacing it cleanly.
    """

    def test_daemon_error_propagates_as_exception(self, monkeypatch):
        async def fake_call_daemon(tool, args, timeout=120.0):
            raise RuntimeError("Not authorized. Run 'telegram-mcp login' first.")

        monkeypatch.setattr(server_module, "_call_daemon", fake_call_daemon)

        with pytest.raises(RuntimeError, match="Not authorized"):
            asyncio.run(call_tool("list_chats", {}))

    def test_timeout_raises_with_named_message(self, monkeypatch):
        async def hang(tool, args, timeout=120.0):
            await asyncio.sleep(10)

        monkeypatch.setattr(server_module, "_call_daemon", hang)
        monkeypatch.setattr(server_module.asyncio, "wait_for", _immediate_timeout)

        with pytest.raises(RuntimeError, match="list_chats.*timed out"):
            asyncio.run(call_tool("list_chats", {}))


async def _immediate_timeout(coro, timeout):
    coro.close()
    raise asyncio.TimeoutError


class TestTerminalSignalDetachment:
    """The stdio proxy must survive terminal job-control signals (SIGHUP on
    terminal hangup, SIGINT on Ctrl-C). Those reach the proxy only because it
    shares the harness's controlling-terminal process group; exiting on them is
    what made the server keep disconnecting when Jean detached zellij, closed
    the terminal, or interrupted Claude. Genuine shutdown comes via stdin EOF or
    SIGTERM, which this does not touch.
    """

    def test_ignores_sighup_and_sigint(self):
        import signal

        from telegram_mcp.server import _ignore_terminal_signals

        prev = {
            signal.SIGHUP: signal.getsignal(signal.SIGHUP),
            signal.SIGINT: signal.getsignal(signal.SIGINT),
            signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        }
        try:
            _ignore_terminal_signals()
            assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
            assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
            # SIGTERM is the harness's explicit stop -- must stay untouched.
            assert signal.getsignal(signal.SIGTERM) == prev[signal.SIGTERM]
        finally:
            for sig, handler in prev.items():
                signal.signal(sig, handler)


class TestToolAnnotations:
    def test_every_tool_has_all_four_hints(self):
        for tool in TOOLS:
            ann = tool.annotations
            assert ann is not None, f"{tool.name} has no annotations"
            for hint in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
                assert isinstance(getattr(ann, hint), bool), f"{tool.name}.{hint} unset"

    def test_write_tools_are_not_marked_read_only(self):
        from telegram_mcp._registry import WRITE_TOOLS

        for tool in TOOLS:
            if tool.name in WRITE_TOOLS or tool.name == "clear_cache":
                assert tool.annotations.readOnlyHint is False, tool.name

    def test_read_only_hint_implies_allowed_in_read_only_mode(self):
        from telegram_mcp._registry import is_read_only_tool

        for tool in TOOLS:
            if tool.annotations.readOnlyHint:
                assert is_read_only_tool(tool.name), tool.name

    def test_pure_reads_are_marked_read_only(self):
        for name in ("list_chats", "read_messages", "search_messages", "get_me", "get_status",
                     "search_regex", "get_participants", "list_forum_topics"):
            tool = next(t for t in TOOLS if t.name == name)
            assert tool.annotations.readOnlyHint is True, name
            assert tool.annotations.destructiveHint is False, name

    def test_removal_tools_are_destructive(self):
        for name in ("delete_chat", "leave_chat", "delete_message", "block_user",
                     "remove_participant", "clear_cache"):
            tool = next(t for t in TOOLS if t.name == name)
            assert tool.annotations.destructiveHint is True, name

    def test_local_tools_are_closed_world(self):
        from telegram_mcp._registry import LOCAL_ONLY_TOOLS

        for tool in TOOLS:
            expected = tool.name not in LOCAL_ONLY_TOOLS
            assert tool.annotations.openWorldHint is expected, tool.name

    def test_disk_writers_are_not_read_only(self):
        for name in ("download_media", "download_chat_media", "export_chat",
                     "export_cached_messages"):
            tool = next(t for t in TOOLS if t.name == name)
            assert tool.annotations.readOnlyHint is False, name
            assert tool.annotations.destructiveHint is False, name

    def test_annotations_survive_serialisation(self):
        dumped = TOOLS[0].model_dump(exclude_none=True)
        assert set(dumped["annotations"]) == {
            "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"
        }


class TestReadOnlyProxy:
    def test_visible_tools_filters_writes(self):
        from telegram_mcp._registry import WRITE_TOOLS
        from telegram_mcp.server import visible_tools

        assert visible_tools(read_only=False) is TOOLS
        ro_names = {t.name for t in visible_tools(read_only=True)}
        assert not (ro_names & WRITE_TOOLS)
        assert "clear_cache" not in ro_names
        assert "list_chats" in ro_names and "read_messages" in ro_names

    def test_list_tools_honours_env(self, monkeypatch):
        from telegram_mcp.server import list_tools

        monkeypatch.setattr(server_module, "_READ_ONLY", True)
        names = {t.name for t in asyncio.run(list_tools())}
        assert "send_message" not in names and "list_chats" in names

        monkeypatch.setattr(server_module, "_READ_ONLY", False)
        assert len(asyncio.run(list_tools())) == len(TOOLS)

    def test_call_tool_refuses_hidden_tool(self, monkeypatch):
        called = []

        async def fake_call_daemon(tool, args, timeout=120.0):
            called.append(tool)
            return {}

        monkeypatch.setattr(server_module, "_call_daemon", fake_call_daemon)
        monkeypatch.setattr(server_module, "_READ_ONLY", True)
        with pytest.raises(RuntimeError, match="not available"):
            asyncio.run(call_tool("send_message", {"chat_id": 1, "text": "x"}))
        assert called == []

    def test_request_payload_carries_read_only_flag(self, monkeypatch, tmp_path):
        """The proxy tags every request so the daemon enforces per session."""
        import json
        import os
        import tempfile
        import uuid

        monkeypatch.setattr(server_module, "_READ_ONLY", True)
        # Short path: macOS caps AF_UNIX paths at 104 chars, tmp_path is longer
        sock = os.path.join(tempfile.gettempdir(), f"tg-mcp-{uuid.uuid4().hex[:8]}.sock")
        monkeypatch.setattr(server_module, "SOCKET_PATH", sock)
        seen = []

        async def handler(reader, writer):
            seen.append(json.loads(await reader.readline()))
            writer.write(b'{"id": 1, "result": "ok"}\n')
            await writer.drain()
            writer.close()

        async def run():
            server = await asyncio.start_unix_server(handler, path=sock)
            try:
                return await server_module._call_daemon("get_me", {})
            finally:
                server.close()
                await server.wait_closed()

        try:
            assert asyncio.run(run()) == "ok"
        finally:
            if os.path.exists(sock):
                os.unlink(sock)
        assert seen[0]["read_only"] is True


class TestSpawnDaemonHygiene:
    def test_config_dir_created_0700_and_read_only_env_inherited(self, monkeypatch, tmp_path):
        """A daemon started from a read-only session must itself be read-only:
        the proxy filter alone can be bypassed by talking to the socket."""
        import os
        import stat

        cfg = tmp_path / "cfg"
        monkeypatch.setattr("telegram_mcp.login.CONFIG_DIR", str(cfg))
        monkeypatch.setenv("TELEGRAM_MCP_READ_ONLY", "1")
        monkeypatch.setenv("KEEP_ME", "yes")
        popen_calls = []

        def fake_popen(cmd, **kwargs):
            popen_calls.append(kwargs)

        monkeypatch.setattr(server_module.subprocess, "Popen", fake_popen)
        server_module._spawn_daemon()

        assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(cfg / "daemon.log").st_mode) == 0o600
        env = popen_calls[0].get("env")
        assert env is None or env.get("TELEGRAM_MCP_READ_ONLY") == "1"


class TestDaemonLogging:
    def test_rotating_handler_and_quiet_telethon(self, tmp_path):
        import logging
        import os
        from logging.handlers import RotatingFileHandler

        root = logging.getLogger()
        before = list(root.handlers)
        telethon_before = logging.getLogger("telethon").level
        log_path = str(tmp_path / "daemon.log")
        try:
            handler = server_module.configure_daemon_logging(log_path)
            assert isinstance(handler, RotatingFileHandler)
            assert handler.maxBytes == server_module.DAEMON_LOG_MAX_BYTES > 0
            assert handler.backupCount >= 1
            assert logging.getLogger("telethon").level == logging.WARNING
            assert logging.getLogger("telethon").isEnabledFor(logging.INFO) is False
            logging.getLogger("telegram_mcp.test").info("hello log")
            handler.flush()
            assert "hello log" in open(log_path).read()
            assert oct(os.stat(log_path).st_mode & 0o777) == "0o600"
        finally:
            for h in list(root.handlers):
                if h not in before:
                    root.removeHandler(h)
                    h.close()
            logging.getLogger("telethon").setLevel(telethon_before)
