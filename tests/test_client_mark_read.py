"""Tests for forum-topic-aware mark_read behaviour."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_mcp.client import TelegramMCPClient


class FakeTelethonClient:
    """Stand-in for the Telethon client.

    Telethon is invoked two ways:
      - As methods (await self._client.get_entity(...))
      - As a callable, sending an MTProto request (await self._client(SomeRequest))

    AsyncMock can't easily cover both shapes, so we hand-roll this stub.
    """

    def __init__(self):
        self.send_read_acknowledge = AsyncMock(return_value=True)
        self.get_entity = AsyncMock()
        self.get_input_entity = AsyncMock()
        self.get_messages = AsyncMock()
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return None


@pytest.fixture
def client(monkeypatch):
    """Build a TelegramMCPClient with all I/O mocked out."""
    monkeypatch.setattr(
        "telegram_mcp.client.load_config",
        lambda: {"api_id": 1, "api_hash": "x", "rate_limits": {}, "upload_dirs": ["/tmp"]},
    )
    monkeypatch.setattr("telegram_mcp.client.MessageCache", MagicMock())
    monkeypatch.setattr("telegram_mcp.client.ensure_dir", lambda p: None)
    monkeypatch.setattr("telegram_mcp.client.TelegramClient", MagicMock())

    inst = TelegramMCPClient()
    inst._client = FakeTelethonClient()
    return inst


class TestMarkRead:
    async def test_chat_only_calls_send_read_acknowledge(self, client):
        entity = MagicMock()
        client._client.get_entity.return_value = entity

        result = await client.mark_read(123)

        client._client.send_read_acknowledge.assert_awaited_once_with(entity)
        assert client._client.requests == []
        assert result == {"status": "marked_read"}

    async def test_topic_id_triggers_read_discussion(self, client):
        from telethon.tl.functions.messages import ReadDiscussionRequest

        entity = MagicMock()
        peer = MagicMock()
        latest_msg = MagicMock()
        latest_msg.id = 9999
        client._client.get_entity.return_value = entity
        client._client.get_input_entity.return_value = peer
        client._client.get_messages.return_value = [latest_msg]

        result = await client.mark_read(1266974497, topic_id=1)

        client._client.send_read_acknowledge.assert_awaited_once_with(entity)
        assert len(client._client.requests) == 1
        req = client._client.requests[0]
        assert isinstance(req, ReadDiscussionRequest)
        assert req.msg_id == 1
        assert req.read_max_id == 9999
        assert result == {"status": "marked_read", "topic_id": 1, "read_max_id": 9999}

    async def test_topic_id_with_no_messages_uses_zero(self, client):
        client._client.get_entity.return_value = MagicMock()
        client._client.get_input_entity.return_value = MagicMock()
        client._client.get_messages.return_value = []

        result = await client.mark_read(123, topic_id=1)

        assert client._client.requests[0].read_max_id == 0
        assert result["read_max_id"] == 0
