"""Tests for forum topic tools: list_forum_topics, send_message+topic_id, read_messages+topic_id."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_mcp.client import TelegramMCPClient


class FakeTelethonClient:
    def __init__(self):
        self.send_message = AsyncMock()
        self.get_messages = AsyncMock(return_value=[])
        self.get_input_entity = AsyncMock()
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return self._response

    _response = None


@pytest.fixture
def client(monkeypatch):
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


class TestListForumTopics:
    async def test_dispatches_get_forum_topics_request(self, client):
        from telethon.tl.functions.messages import GetForumTopicsRequest

        topic1 = MagicMock()
        topic1.id = 5
        topic1.title = "AI"
        topic1.top_message = 1234
        topic1.unread_count = 3
        topic1.closed = False
        topic1.pinned = True
        topic1.hidden = False

        topic2 = MagicMock()
        topic2.id = 9
        topic2.title = "Health"
        topic2.top_message = 5678
        topic2.unread_count = 0
        topic2.closed = False
        topic2.pinned = False
        topic2.hidden = False

        deleted = MagicMock(spec=["id"])
        deleted.id = 99

        response = MagicMock()
        response.topics = [topic1, deleted, topic2]
        client._client._response = response

        peer = MagicMock()
        client._client.get_input_entity.return_value = peer

        result = await client.list_forum_topics(1266974497, limit=50, query="some-q")

        assert len(client._client.requests) == 1
        req = client._client.requests[0]
        assert isinstance(req, GetForumTopicsRequest)
        assert req.peer is peer
        assert req.limit == 50
        assert req.q == "some-q"

        # Deleted topic without title is filtered out
        assert len(result) == 2
        assert result[0]["id"] == 5
        assert result[0]["unread_count"] == 3
        assert result[0]["pinned"] is True
        # title is fenced
        assert "AI" in result[0]["title"]
        assert result[1]["id"] == 9


class TestSendMessageWithTopic:
    async def test_send_with_topic_id_sets_top_msg_id(self, client):
        from telethon.tl.types import InputReplyToMessage

        sent_msg = MagicMock()
        sent_msg.id = 999
        sent_msg.chat_id = -100
        sent_msg.sender = None
        sent_msg.text = "hi"
        sent_msg.date = None
        sent_msg.reply_to = None
        sent_msg.media = None
        sent_msg.edit_date = None
        client._client.send_message.return_value = sent_msg

        await client.send_message(1266974497, "hi", topic_id=1)

        client._client.send_message.assert_awaited_once()
        kwargs = client._client.send_message.call_args.kwargs
        reply_arg = kwargs["reply_to"]
        assert isinstance(reply_arg, InputReplyToMessage)
        assert reply_arg.top_msg_id == 1
        assert reply_arg.reply_to_msg_id == 1

    async def test_send_with_topic_id_and_reply_to_replies_within_topic(self, client):
        from telethon.tl.types import InputReplyToMessage

        sent_msg = MagicMock()
        sent_msg.id = 1000
        sent_msg.chat_id = -100
        sent_msg.sender = None
        sent_msg.text = "reply"
        sent_msg.date = None
        sent_msg.reply_to = None
        sent_msg.media = None
        sent_msg.edit_date = None
        client._client.send_message.return_value = sent_msg

        await client.send_message(1266974497, "reply", reply_to=42, topic_id=5)

        kwargs = client._client.send_message.call_args.kwargs
        reply_arg = kwargs["reply_to"]
        assert isinstance(reply_arg, InputReplyToMessage)
        assert reply_arg.top_msg_id == 5
        assert reply_arg.reply_to_msg_id == 42

    async def test_send_without_topic_id_passes_plain_int(self, client):
        sent_msg = MagicMock()
        sent_msg.id = 1
        sent_msg.chat_id = -1
        sent_msg.sender = None
        sent_msg.text = ""
        sent_msg.date = None
        sent_msg.reply_to = None
        sent_msg.media = None
        sent_msg.edit_date = None
        client._client.send_message.return_value = sent_msg

        await client.send_message(123, "hello", reply_to=77)

        kwargs = client._client.send_message.call_args.kwargs
        # Plain int reply_to is preserved (no InputReplyToMessage wrapping by us)
        assert kwargs["reply_to"] == 77


class TestReadMessagesWithTopic:
    async def test_topic_id_passed_as_reply_to(self, client):
        await client.read_messages(1266974497, topic_id=5, limit=10)

        client._client.get_messages.assert_awaited_once()
        kwargs = client._client.get_messages.call_args.kwargs
        assert kwargs["reply_to"] == 5
        assert kwargs["limit"] == 10

    async def test_no_topic_id_omits_reply_to(self, client):
        await client.read_messages(123, limit=5)

        kwargs = client._client.get_messages.call_args.kwargs
        assert "reply_to" not in kwargs
