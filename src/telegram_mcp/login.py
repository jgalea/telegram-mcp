"""Interactive Telegram login CLI and config management."""

from __future__ import annotations

import json
import os

import click
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from telegram_mcp.security import ensure_dir, secure_write

CONFIG_DIR = os.path.expanduser("~/.telegram-mcp")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
SESSION_PATH = os.path.join(CONFIG_DIR, "session")
DOWNLOADS_DIR = os.path.join(CONFIG_DIR, "downloads")


def load_config() -> dict:
    """Load config from disk, or return empty dict."""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def save_config(config: dict) -> None:
    """Save config to disk with restricted permissions."""
    ensure_dir(CONFIG_DIR)
    secure_write(CONFIG_PATH, json.dumps(config, indent=2))


@click.command("login")
def login_command() -> None:
    """Authenticate with Telegram and create a session."""
    ensure_dir(CONFIG_DIR)
    config = load_config()

    api_id = config.get("api_id")
    api_hash = config.get("api_hash")

    if not api_id or not api_hash:
        click.echo("You need a Telegram API ID and hash from https://my.telegram.org")
        api_id = click.prompt("API ID", type=int)
        api_hash = click.prompt("API Hash", type=str)
        config["api_id"] = api_id
        config["api_hash"] = api_hash
        save_config(config)
        click.echo(f"Config saved to {CONFIG_PATH}")

    client = TelegramClient(SESSION_PATH, api_id, api_hash)

    async def do_login():
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            click.echo(f"Already logged in as {me.first_name} (@{me.username})")
            await client.disconnect()
            return

        phone = click.prompt("Phone number (with country code, e.g. +34...)")
        await client.send_code_request(phone)
        code = click.prompt("Enter the code Telegram sent you")

        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            password = click.prompt("2FA password", hide_input=True)
            await client.sign_in(password=password)

        me = await client.get_me()
        click.echo(f"Logged in as {me.first_name} (@{me.username})")

        session_file = SESSION_PATH + ".session"
        if os.path.exists(session_file):
            os.chmod(session_file, 0o600)

        await client.disconnect()

    import asyncio
    asyncio.run(do_login())
    click.echo("Session saved. You can now use telegram-mcp serve.")
