"""Test that all tools are registered and well-formed."""

from telegram_mcp.server import DESTRUCTIVE_TOOLS, TOOLS


class TestToolRegistration:
    def test_tool_count(self):
        assert len(TOOLS) == 45

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
