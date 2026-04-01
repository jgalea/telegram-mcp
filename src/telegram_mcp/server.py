"""MCP server — tool definitions and stdio entry point."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import click
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from telegram_mcp.client import TelegramMCPClient

app = Server("telegram-mcp")
_client: TelegramMCPClient | None = None

# --- Tool tier classification ---

DESTRUCTIVE_TOOLS = {
    "delete_chat",
    "leave_chat",
    "block_user",
    "remove_participant",
    "delete_message",
    "forward_message",
}


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> Tool:
    """Helper to create a Tool definition."""
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    # Add confirm param for destructive tools
    if name in DESTRUCTIVE_TOOLS:
        schema["properties"]["confirm"] = {
            "type": "boolean",
            "description": (
                "Must be true to execute this destructive action. Without it, returns a warning."
            ),
        }
        description += " (DESTRUCTIVE: requires confirm=true)"
    return Tool(name=name, description=description, inputSchema=schema)


def _text(data: Any) -> list[TextContent]:
    """Format tool output as JSON text content."""
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _error(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": msg}))]


# --- Tool Definitions ---

TOOLS = [
    # Chats
    _tool(
        "list_chats",
        "List all dialogs (groups, channels, DMs) with unread counts",
        {
            "limit": {
                "type": "integer",
                "description": "Max chats to return (default 50)",
                "default": 50,
            }
        },
    ),
    _tool(
        "get_chat_info",
        "Get details for a specific chat",
        {"chat_id": {"type": ["integer", "string"], "description": "Chat ID or @username"}},
        required=["chat_id"],
    ),
    _tool(
        "create_group",
        "Create a new group",
        {
            "title": {"type": "string"},
            "users": {"type": "array", "items": {"type": ["integer", "string"]}},
        },
        required=["title", "users"],
    ),
    _tool(
        "create_channel",
        "Create a new channel",
        {"title": {"type": "string"}, "about": {"type": "string", "default": ""}},
        required=["title"],
    ),
    _tool(
        "archive_chat",
        "Archive or unarchive a chat",
        {
            "chat_id": {"type": ["integer", "string"]},
            "archive": {"type": "boolean", "default": True},
        },
        required=["chat_id"],
    ),
    _tool(
        "mute_chat",
        "Mute or unmute notifications for a chat",
        {
            "chat_id": {"type": ["integer", "string"]},
            "mute": {"type": "boolean", "default": True},
        },
        required=["chat_id"],
    ),
    _tool(
        "leave_chat",
        "Leave a group or channel",
        {"chat_id": {"type": ["integer", "string"]}},
        required=["chat_id"],
    ),
    _tool(
        "delete_chat",
        "Delete a chat",
        {"chat_id": {"type": ["integer", "string"]}},
        required=["chat_id"],
    ),
    _tool(
        "mark_read",
        "Mark a chat as read",
        {"chat_id": {"type": ["integer", "string"]}},
        required=["chat_id"],
    ),
    # Messages: Read
    _tool(
        "read_messages",
        "Get recent messages from a chat",
        {
            "chat_id": {"type": ["integer", "string"]},
            "limit": {"type": "integer", "default": 20},
            "offset_date": {"type": "string", "description": "ISO date to read before"},
            "from_user": {"type": ["integer", "string"]},
        },
        required=["chat_id"],
    ),
    _tool(
        "search_messages",
        "Search messages by keyword, optionally in a specific chat",
        {
            "query": {"type": "string"},
            "chat_id": {"type": ["integer", "string"]},
            "limit": {"type": "integer", "default": 20},
        },
        required=["query"],
    ),
    _tool(
        "get_message",
        "Get a single message by ID",
        {
            "chat_id": {"type": ["integer", "string"]},
            "message_id": {"type": "integer"},
        },
        required=["chat_id", "message_id"],
    ),
    _tool(
        "get_message_replies",
        "Get replies/thread for a message",
        {
            "chat_id": {"type": ["integer", "string"]},
            "message_id": {"type": "integer"},
            "limit": {"type": "integer", "default": 20},
        },
        required=["chat_id", "message_id"],
    ),
    _tool(
        "get_scheduled_messages",
        "List scheduled messages in a chat",
        {"chat_id": {"type": ["integer", "string"]}},
        required=["chat_id"],
    ),
    # Messages: Write
    _tool(
        "send_message",
        "Send a message to a chat (supports reply-to for forum topics)",
        {
            "chat_id": {"type": ["integer", "string"]},
            "text": {"type": "string"},
            "reply_to": {"type": "integer"},
        },
        required=["chat_id", "text"],
    ),
    _tool(
        "edit_message",
        "Edit a sent message",
        {
            "chat_id": {"type": ["integer", "string"]},
            "message_id": {"type": "integer"},
            "text": {"type": "string"},
        },
        required=["chat_id", "message_id", "text"],
    ),
    _tool(
        "delete_message",
        "Delete messages",
        {
            "chat_id": {"type": ["integer", "string"]},
            "message_ids": {"type": "array", "items": {"type": "integer"}},
        },
        required=["chat_id", "message_ids"],
    ),
    _tool(
        "forward_message",
        "Forward messages to another chat",
        {
            "from_chat": {"type": ["integer", "string"]},
            "message_ids": {"type": "array", "items": {"type": "integer"}},
            "to_chat": {"type": ["integer", "string"]},
        },
        required=["from_chat", "message_ids", "to_chat"],
    ),
    _tool(
        "schedule_message",
        "Send a message at a future time",
        {
            "chat_id": {"type": ["integer", "string"]},
            "text": {"type": "string"},
            "schedule_date": {"type": "string", "description": "ISO datetime"},
            "reply_to": {"type": "integer"},
        },
        required=["chat_id", "text", "schedule_date"],
    ),
    _tool(
        "send_reaction",
        "React to a message with an emoji",
        {
            "chat_id": {"type": ["integer", "string"]},
            "message_id": {"type": "integer"},
            "emoji": {"type": "string"},
        },
        required=["chat_id", "message_id", "emoji"],
    ),
    # Messages: Manage
    _tool(
        "pin_message",
        "Pin a message in a chat",
        {
            "chat_id": {"type": ["integer", "string"]},
            "message_id": {"type": "integer"},
        },
        required=["chat_id", "message_id"],
    ),
    _tool(
        "unpin_message",
        "Unpin a message in a chat",
        {
            "chat_id": {"type": ["integer", "string"]},
            "message_id": {"type": "integer"},
        },
        required=["chat_id", "message_id"],
    ),
    # Media
    _tool(
        "download_media",
        "Download a photo, video, or document from a message",
        {
            "chat_id": {"type": ["integer", "string"]},
            "message_id": {"type": "integer"},
        },
        required=["chat_id", "message_id"],
    ),
    _tool(
        "send_file",
        "Send a file or photo to a chat",
        {
            "chat_id": {"type": ["integer", "string"]},
            "file_path": {"type": "string"},
            "caption": {"type": "string", "default": ""},
        },
        required=["chat_id", "file_path"],
    ),
    _tool(
        "send_voice",
        "Send a voice message",
        {
            "chat_id": {"type": ["integer", "string"]},
            "file_path": {"type": "string"},
        },
        required=["chat_id", "file_path"],
    ),
    _tool(
        "send_location",
        "Send a location",
        {
            "chat_id": {"type": ["integer", "string"]},
            "lat": {"type": "number"},
            "lon": {"type": "number"},
        },
        required=["chat_id", "lat", "lon"],
    ),
    _tool("get_sticker_sets", "List available sticker packs", {}),
    # Contacts
    _tool("list_contacts", "List all contacts", {}),
    _tool(
        "get_contact",
        "Get contact details",
        {"user_id": {"type": ["integer", "string"]}},
        required=["user_id"],
    ),
    # Users
    _tool(
        "get_user",
        "Get user profile info",
        {"user_id": {"type": ["integer", "string"]}},
        required=["user_id"],
    ),
    _tool(
        "block_user",
        "Block a user",
        {"user_id": {"type": ["integer", "string"]}},
        required=["user_id"],
    ),
    _tool(
        "unblock_user",
        "Unblock a user",
        {"user_id": {"type": ["integer", "string"]}},
        required=["user_id"],
    ),
    # Groups & Channels
    _tool(
        "get_participants",
        "List members of a group or channel",
        {
            "chat_id": {"type": ["integer", "string"]},
            "limit": {"type": "integer", "default": 100},
        },
        required=["chat_id"],
    ),
    _tool(
        "add_participant",
        "Add a user to a group or channel",
        {
            "chat_id": {"type": ["integer", "string"]},
            "user_id": {"type": ["integer", "string"]},
        },
        required=["chat_id", "user_id"],
    ),
    _tool(
        "remove_participant",
        "Remove a user from a group or channel",
        {
            "chat_id": {"type": ["integer", "string"]},
            "user_id": {"type": ["integer", "string"]},
        },
        required=["chat_id", "user_id"],
    ),
    _tool(
        "set_chat_title",
        "Change a chat's title",
        {
            "chat_id": {"type": ["integer", "string"]},
            "title": {"type": "string"},
        },
        required=["chat_id", "title"],
    ),
    _tool(
        "set_chat_description",
        "Change a chat's description",
        {
            "chat_id": {"type": ["integer", "string"]},
            "description": {"type": "string"},
        },
        required=["chat_id", "description"],
    ),
    _tool(
        "set_chat_photo",
        "Change a chat's photo",
        {
            "chat_id": {"type": ["integer", "string"]},
            "file_path": {"type": "string"},
        },
        required=["chat_id", "file_path"],
    ),
    _tool(
        "get_invite_link",
        "Generate an invite link for a group or channel",
        {"chat_id": {"type": ["integer", "string"]}},
        required=["chat_id"],
    ),
    _tool(
        "get_admin_log",
        "Get admin action history for a group or channel",
        {
            "chat_id": {"type": ["integer", "string"]},
            "limit": {"type": "integer", "default": 50},
        },
        required=["chat_id"],
    ),
    # Account & Utility
    _tool("get_me", "Get current account info", {}),
    _tool("get_status", "Get connection status and session health", {}),
    _tool("get_dialogs_stats", "Get unread counts and chat activity summary", {}),
    _tool(
        "export_chat",
        "Export messages from a chat as JSON (max 1000 per call)",
        {
            "chat_id": {"type": ["integer", "string"]},
            "limit": {"type": "integer", "default": 1000},
        },
        required=["chat_id"],
    ),
    _tool("clear_cache", "Wipe the local message cache", {}),
]


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    global _client
    if _client is None:
        return _error("Not connected. Server failed to initialize.")

    # Destructive tool gate
    if name in DESTRUCTIVE_TOOLS and not arguments.get("confirm"):
        return _text(
            {
                "warning": (
                    f"'{name}' is a destructive action. "
                    "Call again with confirm=true to proceed."
                ),
                "would_do": f"Execute {name} with args: {arguments}",
            }
        )

    try:
        method = getattr(_client, name, None)
        if method is None:
            return _error(f"Unknown tool: {name}")

        # Remove 'confirm' from args before passing to client
        args = {k: v for k, v in arguments.items() if k != "confirm"}
        result = await method(**args)
        return _text(result)
    except Exception as e:
        return _error(str(e))


async def serve() -> None:
    """Start the MCP server on stdio."""
    global _client
    _client = TelegramMCPClient()
    await _client.connect()

    try:
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())
    finally:
        await _client.disconnect()


@click.group()
def main_cli():
    """telegram-mcp -- Telegram MCP server."""


@main_cli.command("serve")
def serve_cmd():
    """Start the MCP server on stdio."""
    asyncio.run(serve())


@main_cli.command("login")
def login_cmd():
    """Authenticate with Telegram."""
    from telegram_mcp.login import login_command

    login_command.main(standalone_mode=False)


if __name__ == "__main__":
    main_cli()
