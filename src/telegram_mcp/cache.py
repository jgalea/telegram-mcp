"""SQLite message cache for telegram-mcp.

Stores messages and chat metadata passively as they are fetched from Telegram.
Used for local search and deduplication — not a sync engine.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    sender_id INTEGER,
    sender_name TEXT,
    text TEXT,
    date TEXT NOT NULL,
    reply_to_id INTEGER,
    media_type TEXT,
    edited TEXT,
    raw_json TEXT,
    PRIMARY KEY (id, chat_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_date ON messages(chat_id, date);
CREATE INDEX IF NOT EXISTS idx_messages_text ON messages(text);
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY,
    name TEXT,
    type TEXT,
    last_seen TEXT
);
"""


class MessageCache:
    """Lightweight SQLite cache for Telegram messages and chat metadata."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        is_new = not os.path.exists(db_path)

        if is_new:
            # Create the file with restricted permissions before SQLite opens it.
            fd = os.open(db_path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)

        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def cache_message(
        self,
        msg_id: int,
        chat_id: int,
        sender_id: int | None,
        sender_name: str | None,
        text: str | None,
        date: str,
        reply_to_id: int | None,
        media_type: str | None,
        edited: str | None,
        raw_json: str | None,
    ) -> None:
        """Insert or update a message in the cache."""
        self._conn.execute(
            """
            INSERT INTO messages (id, chat_id, sender_id, sender_name, text, date,
                                  reply_to_id, media_type, edited, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id, chat_id) DO UPDATE SET
                text = excluded.text,
                edited = excluded.edited,
                raw_json = excluded.raw_json
            """,
            (msg_id, chat_id, sender_id, sender_name, text, date,
             reply_to_id, media_type, edited, raw_json),
        )
        self._conn.commit()

    def search(
        self,
        query: str,
        chat_id: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Search messages by text using LIKE, optionally filtered by chat_id."""
        sql = "SELECT * FROM messages WHERE text LIKE ?"
        params: list = [f"%{query}%"]

        if chat_id is not None:
            sql += " AND chat_id = ?"
            params.append(chat_id)

        sql += " ORDER BY date DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_message_ids(self, chat_id: int, msg_ids: list[int]) -> set[int]:
        """Return the subset of msg_ids that already exist in cache for a chat."""
        if not msg_ids:
            return set()
        placeholders = ",".join("?" for _ in msg_ids)
        rows = self._conn.execute(
            f"SELECT id FROM messages WHERE chat_id = ? AND id IN ({placeholders})",
            [chat_id, *msg_ids],
        ).fetchall()
        return {row["id"] for row in rows}

    # ------------------------------------------------------------------
    # Chats
    # ------------------------------------------------------------------

    def cache_chat(self, chat_id: int, name: str, chat_type: str) -> None:
        """Insert or update chat metadata with current timestamp as last_seen."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO chats (id, name, type, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                type = excluded.type,
                last_seen = excluded.last_seen
            """,
            (chat_id, name, chat_type, now),
        )
        self._conn.commit()

    def get_cached_chats(self) -> list[dict]:
        """Return all cached chats ordered by last_seen descending."""
        rows = self._conn.execute(
            "SELECT * FROM chats ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def prune(self, max_age_days: int) -> int:
        """Delete messages older than *max_age_days* and return the count removed."""
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        cursor = self._conn.execute("DELETE FROM messages WHERE date < ?", (cutoff,))
        self._conn.commit()
        return cursor.rowcount

    def clear(self) -> None:
        """Delete all messages and chats from the cache."""
        self._conn.execute("DELETE FROM messages")
        self._conn.execute("DELETE FROM chats")
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
