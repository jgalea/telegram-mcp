"""Tests for the message cache module."""

import os
import stat
from sqlite3 import ProgrammingError

import pytest

from telegram_mcp.cache import MessageCache


@pytest.fixture
def cache(tmp_path):
    """Create a MessageCache with a temporary database."""
    db_path = tmp_path / "test_cache.db"
    c = MessageCache(str(db_path))
    yield c
    c.close()


@pytest.fixture
def db_path(tmp_path):
    """Return a path for a database file (without creating a cache)."""
    return tmp_path / "test_cache.db"


class TestCacheCreation:
    def test_creates_database_file(self, db_path):
        """MessageCache(db_path) creates the database file on disk."""
        cache = MessageCache(str(db_path))
        try:
            assert db_path.exists()
        finally:
            cache.close()

    def test_has_messages_table(self, cache, tmp_path):
        """Created DB has a messages table."""
        rows = cache._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchall()
        assert len(rows) == 1

    def test_has_chats_table(self, cache, tmp_path):
        """Created DB has a chats table."""
        rows = cache._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chats'"
        ).fetchall()
        assert len(rows) == 1

    def test_file_permissions(self, db_path):
        """DB file has 0o600 permissions (owner read/write only)."""
        cache = MessageCache(str(db_path))
        try:
            file_stat = os.stat(str(db_path))
            mode = stat.S_IMODE(file_stat.st_mode)
            assert mode == 0o600
        finally:
            cache.close()


class TestCacheMessage:
    def test_cache_and_search(self, cache):
        """cache_message() stores a message that is searchable via search()."""
        cache.cache_message(
            msg_id=1,
            chat_id=100,
            sender_id=200,
            sender_name="Alice",
            text="Hello world",
            date="2026-03-31T10:00:00",
            reply_to_id=None,
            media_type=None,
            edited=None,
            raw_json='{"id": 1}',
        )
        results = cache.search("Hello")
        assert len(results) == 1
        assert results[0]["text"] == "Hello world"
        assert results[0]["sender_name"] == "Alice"
        assert results[0]["chat_id"] == 100

    def test_upsert_updates_text_and_edited(self, cache):
        """cache_message() with the same ID upserts (updates text and edited)."""
        cache.cache_message(
            msg_id=1,
            chat_id=100,
            sender_id=200,
            sender_name="Alice",
            text="Original text",
            date="2026-03-31T10:00:00",
            reply_to_id=None,
            media_type=None,
            edited=None,
            raw_json='{"id": 1}',
        )
        cache.cache_message(
            msg_id=1,
            chat_id=100,
            sender_id=200,
            sender_name="Alice",
            text="Updated text",
            date="2026-03-31T10:00:00",
            reply_to_id=None,
            media_type=None,
            edited="2026-03-31T11:00:00",
            raw_json='{"id": 1, "edited": true}',
        )
        results = cache.search("Updated")
        assert len(results) == 1
        assert results[0]["text"] == "Updated text"
        assert results[0]["edited"] == "2026-03-31T11:00:00"

        # Confirm there is only one row, not two
        all_results = cache.search("text")
        assert len(all_results) == 1


class TestCacheChat:
    def test_cache_and_get_chats(self, cache):
        """cache_chat() stores chat metadata retrievable via get_cached_chats()."""
        cache.cache_chat(chat_id=100, name="Test Group", chat_type="group")
        chats = cache.get_cached_chats()
        assert len(chats) == 1
        assert chats[0]["id"] == 100
        assert chats[0]["name"] == "Test Group"
        assert chats[0]["type"] == "group"
        assert chats[0]["last_seen"] is not None


class TestSearch:
    def test_search_nonexistent_returns_empty(self, cache):
        """search() for nonexistent term returns empty list."""
        results = cache.search("nonexistent")
        assert results == []

    def test_search_respects_limit(self, cache):
        """search() with limit=3 returns at most 3 results."""
        for i in range(10):
            cache.cache_message(
                msg_id=i,
                chat_id=100,
                sender_id=200,
                sender_name="Alice",
                text=f"Message number {i}",
                date=f"2026-03-31T10:{i:02d}:00",
                reply_to_id=None,
                media_type=None,
                edited=None,
                raw_json=f'{{"id": {i}}}',
            )
        results = cache.search("Message", limit=3)
        assert len(results) == 3

    def test_search_with_chat_id_filter(self, cache):
        """search() with chat_id filters to only that chat."""
        cache.cache_message(
            msg_id=1, chat_id=100, sender_id=200, sender_name="Alice",
            text="Hello from chat 100", date="2026-03-31T10:00:00",
            reply_to_id=None, media_type=None, edited=None, raw_json="{}",
        )
        cache.cache_message(
            msg_id=2, chat_id=200, sender_id=300, sender_name="Bob",
            text="Hello from chat 200", date="2026-03-31T10:01:00",
            reply_to_id=None, media_type=None, edited=None, raw_json="{}",
        )
        results = cache.search("Hello", chat_id=100)
        assert len(results) == 1
        assert results[0]["chat_id"] == 100

    def test_search_ordered_by_date_desc(self, cache):
        """search() returns results ordered by date descending (newest first)."""
        cache.cache_message(
            msg_id=1, chat_id=100, sender_id=200, sender_name="Alice",
            text="Older message", date="2026-03-30T10:00:00",
            reply_to_id=None, media_type=None, edited=None, raw_json="{}",
        )
        cache.cache_message(
            msg_id=2, chat_id=100, sender_id=200, sender_name="Alice",
            text="Newer message", date="2026-03-31T10:00:00",
            reply_to_id=None, media_type=None, edited=None, raw_json="{}",
        )
        results = cache.search("message")
        assert results[0]["text"] == "Newer message"
        assert results[1]["text"] == "Older message"


class TestGetMessageIds:
    def test_returns_existing_ids(self, cache):
        """get_message_ids() returns the set of IDs that exist in cache."""
        for i in [1, 2, 3]:
            cache.cache_message(
                msg_id=i, chat_id=100, sender_id=200, sender_name="Alice",
                text=f"Msg {i}", date="2026-03-31T10:00:00",
                reply_to_id=None, media_type=None, edited=None, raw_json="{}",
            )
        existing = cache.get_message_ids(chat_id=100, msg_ids=[1, 2, 5, 99])
        assert existing == {1, 2}


class TestPrune:
    def test_prune_removes_old_messages(self, cache):
        """prune() deletes messages older than max_age_days."""
        # Old message — 90 days ago
        cache.cache_message(
            msg_id=1, chat_id=100, sender_id=200, sender_name="Alice",
            text="Old message", date="2025-12-01T10:00:00+00:00",
            reply_to_id=None, media_type=None, edited=None, raw_json="{}",
        )
        # Recent message — today
        cache.cache_message(
            msg_id=2, chat_id=100, sender_id=200, sender_name="Alice",
            text="Recent message", date="2026-03-31T10:00:00+00:00",
            reply_to_id=None, media_type=None, edited=None, raw_json="{}",
        )
        removed = cache.prune(max_age_days=30)
        assert removed == 1
        results = cache.search("message")
        assert len(results) == 1
        assert results[0]["text"] == "Recent message"

    def test_prune_keeps_all_when_none_old(self, cache):
        """prune() with no old messages removes nothing."""
        cache.cache_message(
            msg_id=1, chat_id=100, sender_id=200, sender_name="Alice",
            text="Fresh", date="2026-03-31T10:00:00+00:00",
            reply_to_id=None, media_type=None, edited=None, raw_json="{}",
        )
        removed = cache.prune(max_age_days=30)
        assert removed == 0
        assert len(cache.search("Fresh")) == 1

    def test_prune_empty_cache(self, cache):
        """prune() on an empty cache returns 0."""
        assert cache.prune(max_age_days=30) == 0


class TestClear:
    def test_clear_wipes_all_data(self, cache):
        """clear() removes all messages and chats."""
        cache.cache_message(
            msg_id=1, chat_id=100, sender_id=200, sender_name="Alice",
            text="Hello", date="2026-03-31T10:00:00",
            reply_to_id=None, media_type=None, edited=None, raw_json="{}",
        )
        cache.cache_chat(chat_id=100, name="Test", chat_type="group")

        cache.clear()

        assert cache.search("Hello") == []
        assert cache.get_cached_chats() == []


class TestClose:
    def test_close_closes_connection(self, tmp_path):
        """close() closes the DB connection."""
        db_path = tmp_path / "test_close.db"
        c = MessageCache(str(db_path))
        c.close()
        # After close, attempting to use the connection should raise ProgrammingError
        with pytest.raises(ProgrammingError):
            c._conn.execute("SELECT 1")
