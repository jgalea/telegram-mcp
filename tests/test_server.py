"""Test that all tools are registered and well-formed."""

from telegram_mcp.server import DESTRUCTIVE_TOOLS, TOOLS


class TestToolRegistration:
    def test_tool_count(self):
        assert len(TOOLS) == 53

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
