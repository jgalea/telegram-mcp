"""Search must preserve Telegram's per-chat message identity and chat scope."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon import utils
from telethon.tl.types import Channel, ChatPhotoEmpty, Message, User

from telegram_mcp.client import TelegramMCPClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "telegram_mcp.client.load_config",
        lambda: {"api_id": 1, "api_hash": "test", "rate_limits": {}},
    )
    monkeypatch.setattr("telegram_mcp.client.TelegramClient", MagicMock())
    monkeypatch.setattr("telegram_mcp.client.CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("telegram_mcp.client.DOWNLOADS_DIR", str(tmp_path / "downloads"))
    instance = TelegramMCPClient()
    instance._client = MagicMock()
    instance._client.get_entity = AsyncMock()
    instance._client.get_messages = AsyncMock(return_value=[])
    instance._client.get_dialogs = AsyncMock(return_value=[])
    yield instance
    instance._cache.close()


def _cached(client, chat_id: int, message_id: int, date: str = "2026-10-08T12:00:00+00:00"):
    client._cache.cache_message(
        msg_id=message_id, chat_id=chat_id, sender_id=None, sender_name=None,
        text="keyword", date=date, reply_to_id=None, media_type=None,
        edited=None, raw_json=None,
    )


def _live(chat_id: int, message_id: int):
    message = MagicMock(spec=Message)
    message.id = message_id
    message.chat_id = chat_id
    message.text = "keyword"
    message.sender = None
    message.date = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    message.reply_to = None
    message.edit_date = None
    message.media = None
    message.fwd_from = None
    return message


def _identity(messages):
    return {(message["chat_id"], message["id"]) for message in messages}


async def test_username_scoped_search_excludes_cached_messages_from_other_chats(client):
    client._client.get_entity.return_value = User(id=101)
    _cached(client, 101, 10)
    _cached(client, 202, 20)

    result = await client.search_messages("keyword", chat_id="@alice")

    assert _identity(result) == {(101, 10)}


async def test_channel_username_scoped_search_uses_marked_peer_id(client):
    channel = Channel(
        id=300, title="Updates", photo=ChatPhotoEmpty(),
        date=datetime.now(timezone.utc), broadcast=True,
    )
    client._client.get_entity.return_value = channel
    marked_id = utils.get_peer_id(channel)
    _cached(client, marked_id, 11)
    _cached(client, 101, 20)

    result = await client.search_messages("keyword", chat_id="@updates")

    assert _identity(result) == {(marked_id, 11)}


async def test_chat_type_search_excludes_other_types_from_cache(client):
    channel = Channel(
        id=300, title="Updates", photo=ChatPhotoEmpty(),
        date=datetime.now(timezone.utc), broadcast=True,
    )
    channel_dialog = MagicMock()
    channel_dialog.entity = channel
    user_dialog = MagicMock()
    user_dialog.entity = User(id=101)
    client._client.get_dialogs.return_value = [channel_dialog, user_dialog]
    _cached(client, utils.get_peer_id(channel), 11)
    _cached(client, 101, 20)

    result = await client.search_messages("keyword", chat_type="channel")

    assert _identity(result) == {(utils.get_peer_id(channel), 11)}


async def test_unscoped_search_keeps_equal_message_ids_from_distinct_chats(client):
    client._client.get_messages.return_value = [_live(chat_id=101, message_id=7)]
    _cached(client, 202, 7)

    result = await client.search_messages("keyword")

    assert _identity(result) == {(101, 7), (202, 7)}


async def test_same_chat_live_and_cached_message_is_returned_once(client):
    client._client.get_messages.return_value = [_live(chat_id=101, message_id=7)]
    _cached(client, 101, 7)

    result = await client.search_messages("keyword")

    assert len(result) == 1
    assert _identity(result) == {(101, 7)}


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("search_regex", {"pattern": "keyword"}),
        ("message_timeline", {}),
        ("today_messages", {}),
        ("chat_analytics", {}),
        ("export_cached_messages", {}),
    ],
)
async def test_cache_only_tools_do_not_treat_username_scope_as_global(client, method, kwargs):
    _cached(client, 101, 7)

    with pytest.raises(ValueError, match="numeric chat_id"):
        await getattr(client, method)(chat_id="@alice", **kwargs)


async def test_chat_type_with_no_matching_dialogs_returns_no_cached_results(client):
    _cached(client, 101, 7)

    assert await client.search_messages("keyword", chat_type="channel") == []
