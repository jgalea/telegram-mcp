"""Tool responses and the cache must agree on Telethon's marked peer IDs."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon import utils
from telethon.tl.types import Channel, Chat, ChatPhotoEmpty, Message, User

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
    instance._client.get_dialogs = AsyncMock(return_value=[])
    yield instance
    instance._cache.close()


def _entity(kind: str):
    now = datetime.now(timezone.utc)
    if kind == "user":
        return User(id=101, first_name="Alice")
    if kind == "group":
        return Chat(
            id=202, title="Friends", photo=ChatPhotoEmpty(), participants_count=3,
            date=now, version=1,
        )
    return Channel(
        id=300, title="Updates", photo=ChatPhotoEmpty(), date=now, broadcast=True,
    )


def _message(entity, message_id: int):
    message = MagicMock(spec=Message)
    message.id = message_id
    message.chat_id = utils.get_peer_id(entity)
    message.text = "hello"
    message.message = "hello"
    message.sender = None
    message.date = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    message.reply_to = None
    message.edit_date = None
    message.media = None
    message.fwd_from = None
    return message


def _iter_messages(entity, ids):
    calls = []

    def iter_messages(ent, limit=None, min_id=0):
        calls.append(min_id)

        async def gen():
            for i in ids:
                if i > min_id:
                    yield _message(entity, i)

        return gen()

    return iter_messages, calls


KINDS = ["user", "group", "channel"]


@pytest.mark.parametrize("kind", KINDS)
async def test_list_chats_returns_peer_id_and_caches_under_it(client, kind):
    entity = _entity(kind)
    dialog = MagicMock(entity=entity, unread_count=0)
    dialog.name = "Name"
    client._client.get_dialogs.return_value = [dialog]

    result = await client.list_chats()

    marked = utils.get_peer_id(entity)
    assert result[0]["id"] == entity.id
    assert result[0]["peer_id"] == marked
    assert [c["id"] for c in client._cache.get_cached_chats()] == [marked]


@pytest.mark.parametrize("kind", KINDS)
async def test_get_chat_info_returns_peer_id(client, kind):
    entity = _entity(kind)
    client._client.get_entity.return_value = entity

    info = await client.get_chat_info(entity.id)

    assert info["id"] == entity.id
    assert info["peer_id"] == utils.get_peer_id(entity)


@pytest.mark.parametrize("kind", KINDS)
async def test_second_sync_chat_reuses_cached_cursor(client, kind):
    entity = _entity(kind)
    client._client.get_entity.return_value = entity
    client._client.iter_messages, calls = _iter_messages(entity, [5, 7])

    first = await client.sync_chat(utils.get_peer_id(entity))
    second = await client.sync_chat(utils.get_peer_id(entity))

    marked = utils.get_peer_id(entity)
    assert calls == [0, 7]
    assert first["messages_synced"] == 2
    assert second["messages_synced"] == 0
    assert second["peer_id"] == marked
    assert client._cache.get_last_msg_id(marked) == 7
    assert [c["id"] for c in client._cache.get_cached_chats()] == [marked]


@pytest.mark.parametrize("kind", KINDS)
async def test_second_sync_messages_reuses_cached_cursor(client, kind):
    entity = _entity(kind)
    dialog = MagicMock(entity=entity, unread_count=0)
    dialog.name = "Name"
    client._client.get_dialogs.return_value = [dialog]
    client._client.iter_messages, calls = _iter_messages(entity, [5, 7])

    await client.sync_messages()
    result = await client.sync_messages()

    assert calls == [0, 7]
    assert result["total_messages"] == 0
    assert result["details"][0]["peer_id"] == utils.get_peer_id(entity)


async def test_existing_cache_rows_under_marked_id_set_the_cursor(client):
    entity = _entity("channel")
    marked = utils.get_peer_id(entity)
    assert marked != entity.id
    client._cache.cache_message(
        msg_id=9, chat_id=marked, sender_id=None, sender_name=None, text="old",
        date="2026-10-01T00:00:00+00:00", reply_to_id=None, media_type=None,
        edited=None, raw_json=None,
    )
    client._client.get_entity.return_value = entity
    client._client.iter_messages, calls = _iter_messages(entity, [9, 12])

    result = await client.sync_chat(marked)

    assert calls == [9]
    assert result["messages_synced"] == 1
