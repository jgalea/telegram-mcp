"""Telethon wrapper — all Telegram API calls."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from telethon import TelegramClient, utils
from telethon.tl.functions.channels import (
    EditBannedRequest,
    EditPhotoRequest,
    EditTitleRequest,
    GetAdminLogRequest,
    GetParticipantsRequest,
    InviteToChannelRequest,
)
from telethon.tl.functions.contacts import (
    BlockRequest,
    GetContactsRequest,
    UnblockRequest,
)
from telethon.tl.functions.messages import (
    ExportChatInviteRequest,
    GetScheduledHistoryRequest,
    SendReactionRequest,
)
from telethon.tl.types import (
    Channel,
    ChannelParticipantsSearch,
    Chat,
    ChatBannedRights,
    Message,
    MessageMediaDocument,
    MessageMediaGeo,
    MessageMediaPhoto,
    ReactionEmoji,
    User,
)

from telegram_mcp.cache import (
    MessageCache,
    compile_search_pattern,
    regex_filter,
)
from telegram_mcp.login import CONFIG_DIR, DOWNLOADS_DIR, SESSION_PATH, load_config
from telegram_mcp.security import (
    RateLimiter,
    create_unique_file,
    ensure_dir,
    fence,
    is_path_allowed,
    validate_chat_id,
    validate_message_length,
)

logger = logging.getLogger(__name__)


def _forward_info(msg: Message) -> tuple[str | None, int | None]:
    """(display name, peer id) of the original sender of a forwarded message."""
    fwd = getattr(msg, "fwd_from", None)
    if fwd is None:
        return None, None
    name = getattr(fwd, "from_name", None) or getattr(fwd, "post_author", None)
    peer = getattr(fwd, "from_id", None)
    peer_id = None
    for attr in ("user_id", "channel_id", "chat_id"):
        val = getattr(peer, attr, None)
        if isinstance(val, int):
            peer_id = val
            break
    return (name if isinstance(name, str) else None), peer_id


def _msg_to_dict(msg: Message) -> dict[str, Any]:
    """Convert a Telethon Message to a serializable dict."""
    sender = msg.sender
    sender_name = None
    sender_id = None
    if sender:
        sender_id = sender.id
        if isinstance(sender, User):
            full_name = f"{sender.first_name or ''} {sender.last_name or ''}".strip()
            sender_name = full_name or sender.username
        elif hasattr(sender, "title"):
            sender_name = sender.title

    media_type = None
    if msg.media:
        if isinstance(msg.media, MessageMediaPhoto):
            media_type = "photo"
        elif isinstance(msg.media, MessageMediaDocument):
            media_type = "document"
        elif isinstance(msg.media, MessageMediaGeo):
            media_type = "location"
        else:
            media_type = type(msg.media).__name__

    forward_from, forward_from_id = _forward_info(msg)

    result = {
        "id": msg.id,
        "chat_id": msg.chat_id,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "text": msg.text or "",
        "date": msg.date.isoformat() if msg.date else "",
        "reply_to_id": msg.reply_to.reply_to_msg_id if msg.reply_to else None,
        "media_type": media_type,
        "edited": msg.edit_date.isoformat() if msg.edit_date else None,
    }
    if forward_from is not None or forward_from_id is not None:
        result["forward_from"] = forward_from
        result["forward_from_id"] = forward_from_id
    return result


def _fence_message(msg_dict: dict[str, Any]) -> dict[str, Any]:
    """Apply content fencing to a message dict.

    The text of a media message is its caption, so it gets the CAPTION label.
    """
    text_label = "caption" if msg_dict.get("media_type") else "message"
    fenced = {
        **msg_dict,
        "text": fence(msg_dict.get("text"), text_label),
        "sender_name": fence(msg_dict.get("sender_name"), "sender"),
    }
    if "forward_from" in msg_dict:
        fenced["forward_from"] = fence(msg_dict.get("forward_from"), "forward")
    return fenced


def _cache_only_chat_id(chat_id: int | str | None) -> int | None:
    """Never turn an unresolved username scope into an unscoped cache query."""
    if chat_id is None:
        return None
    normalized = validate_chat_id(chat_id)
    if not isinstance(normalized, int):
        raise ValueError(
            "Cache-only tools require a numeric chat_id; use list_chats to find the chat ID"
        )
    return normalized


def _export_summary(messages: list[dict[str, Any]], path: str, fmt: str) -> dict[str, Any]:
    """What an export tool returns: where the file is, plus fenced metadata."""
    dates = [m.get("date") for m in messages if m.get("date")]
    senders = sorted({m.get("sender_name") for m in messages if m.get("sender_name")})
    return {
        "path": path,
        "format": fmt,
        "count": len(messages),
        "date_from": min(dates) if dates else None,
        "date_to": max(dates) if dates else None,
        "senders": [fence(s, "sender") for s in senders[:50]],
        "note": (
            "Full export written to disk. Message bodies are not returned to the model;"
            " read the file with a tool of your own if you need them."
        ),
    }


def _export_filename(prefix: str, ext: str) -> str:
    return f"{prefix}_{datetime.now().strftime('%Y%m%dT%H%M%S')}.{ext}"


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _package_info() -> dict[str, Any]:
    """Version, source location and build time of the running code.

    Makes a stale install visible: the tool is often reinstalled from a
    local checkout without a version bump, so the git sha (when the code
    runs from a checkout) and the on-disk mtime of this file are what
    actually change between installs.
    """
    import importlib.metadata as md  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    try:
        version = md.version("telegram-mcp-jgalea")
    except md.PackageNotFoundError:
        version = "unknown"

    source_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(__file__)).isoformat(timespec="seconds")
    except OSError:
        mtime = None

    git_sha = None
    try:
        out = subprocess.run(
            ["git", "-C", source_dir, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            git_sha = out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass

    return {
        "version": version,
        "git_sha": git_sha,
        "source_dir": source_dir,
        "source_mtime": mtime,
    }


class TelegramMCPClient:
    """High-level wrapper around Telethon for MCP tool use."""

    def __init__(self):
        config = load_config()
        self._api_id = config.get("api_id")
        self._api_hash = config.get("api_hash")
        if not self._api_id or not self._api_hash:
            raise RuntimeError("Not configured. Run 'telegram-mcp login' first.")

        # Explicit connection params so behaviour doesn't drift with Telethon
        # releases. The daemon is long-running, so we want generous retries
        # and a longer per-request timeout for flaky networks.
        self._client = TelegramClient(
            SESSION_PATH,
            self._api_id,
            self._api_hash,
            timeout=30,
            request_retries=5,
            connection_retries=10,
            retry_delay=2,
            auto_reconnect=True,
        )
        self._cache = MessageCache(os.path.join(CONFIG_DIR, "cache.db"))
        self._connected = False

        # Rate limiters
        rl_config = config.get("rate_limits", {})
        self._rl_fetch = RateLimiter(rl_config.get("fetch", 30), 1.0)
        self._rl_search = RateLimiter(rl_config.get("search", 10), 1.0)
        self._rl_write = RateLimiter(rl_config.get("write", 20), 1.0)

        # Upload allowlist
        self._upload_dirs = config.get("upload_dirs", [
            os.path.expanduser("~/Downloads"),
            os.path.expanduser("~/Desktop"),
            os.path.expanduser("~/Documents"),
        ])

        # Stale cache cleanup
        cache_max_age = config.get("cache_max_age_days")
        if cache_max_age is not None:
            self._cache.prune(int(cache_max_age))

        ensure_dir(DOWNLOADS_DIR)

    async def connect(self) -> None:
        """Connect to Telegram."""
        if not self._connected:
            await self._client.connect()
            if not await self._client.is_user_authorized():
                raise RuntimeError("Not authorized. Run 'telegram-mcp login' first.")
            self._connected = True
            self._start_listener()

    async def ensure_connected(self) -> None:
        """Reconnect if the Telegram connection has dropped."""
        if self._client.is_connected():
            return
        logger.warning("Telegram connection lost, reconnecting...")
        self._connected = False
        for attempt in range(3):
            try:
                await self._client.connect()
                if await self._client.is_user_authorized():
                    self._connected = True
                    logger.info("Reconnected to Telegram (attempt %d)", attempt + 1)
                    return
            except Exception as e:
                logger.warning("Reconnect attempt %d failed: %s", attempt + 1, e)
                if attempt < 2:
                    import asyncio
                    await asyncio.sleep(2 ** attempt)
        raise ConnectionError("Failed to reconnect to Telegram after 3 attempts")

    def _start_listener(self) -> None:
        """Register a Telethon event handler that caches all incoming messages."""
        from telethon import events

        @self._client.on(events.NewMessage)
        async def _on_new_message(event):
            msg = event.message
            if not isinstance(msg, Message):
                return
            if msg.text is None and msg.message is None:
                return
            try:
                msg_dict = _msg_to_dict(msg)
                self._cache_messages([msg_dict])
            except Exception as e:
                logger.debug("Cache error on incoming message: %s", e)

    async def disconnect(self) -> None:
        """Disconnect from Telegram."""
        if self._connected:
            await self._client.disconnect()
            self._connected = False
        self._cache.close()

    def _cache_messages(self, messages: list[dict[str, Any]]) -> None:
        """Write-through cache for messages."""
        for msg in messages:
            self._cache.cache_message(
                msg_id=msg["id"], chat_id=msg["chat_id"],
                sender_id=msg.get("sender_id"), sender_name=msg.get("sender_name"),
                text=msg.get("text", ""), date=msg["date"],
                reply_to_id=msg.get("reply_to_id"), media_type=msg.get("media_type"),
                edited=msg.get("edited"), raw_json=json.dumps(msg),
            )

    # --- Chats ---

    async def list_chats(self, limit: int = 50) -> list[dict[str, Any]]:
        self._rl_fetch.acquire()
        dialogs = await self._client.get_dialogs(limit=limit)
        result = []
        for d in dialogs:
            chat_type = "user"
            if isinstance(d.entity, Channel):
                chat_type = "channel" if d.entity.broadcast else "group"
            elif isinstance(d.entity, Chat):
                chat_type = "group"
            info = {
                "id": d.entity.id,
                "name": fence(d.name, "title"),
                "type": chat_type,
                "unread_count": d.unread_count,
            }
            result.append(info)
            self._cache.cache_chat(d.entity.id, d.name, chat_type)
        return result

    async def get_chat_info(self, chat_id: int | str) -> dict[str, Any]:
        self._rl_fetch.acquire()
        chat_id = validate_chat_id(chat_id)
        entity = await self._client.get_entity(chat_id)
        info: dict[str, Any] = {"id": entity.id}

        if isinstance(entity, User):
            info.update({
                "type": "user",
                "name": fence(
                    f"{entity.first_name or ''} {entity.last_name or ''}".strip(), "sender"
                ),
                "username": entity.username,
                "phone": entity.phone,
                "bio": fence(getattr(entity, "about", None), "bio"),
            })
        elif isinstance(entity, (Chat, Channel)):
            info.update({
                "type": (
                    "channel" if (isinstance(entity, Channel) and entity.broadcast) else "group"
                ),
                "name": fence(entity.title, "title"),
                "username": getattr(entity, "username", None),
                "members_count": getattr(entity, "participants_count", None),
                "description": fence(getattr(entity, "about", None), "bio"),
            })
        return info

    async def create_group(self, title: str, users: list[int | str]) -> dict[str, Any]:
        self._rl_write.acquire()
        result = await self._client.create_group(title, users)
        return {"id": result.chats[0].id, "title": title}

    async def create_channel(self, title: str, about: str = "") -> dict[str, Any]:
        self._rl_write.acquire()
        result = await self._client.create_channel(title, about)
        return {"id": result.chats[0].id, "title": title}

    async def archive_chat(self, chat_id: int | str, archive: bool = True) -> dict[str, str]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        entity = await self._client.get_entity(chat_id)
        await self._client.edit_folder(entity, folder=1 if archive else 0)
        return {"status": "archived" if archive else "unarchived"}

    async def mute_chat(self, chat_id: int | str, mute: bool = True) -> dict[str, str]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        from telethon.tl.functions.account import UpdateNotifySettingsRequest
        from telethon.tl.types import InputNotifyPeer, InputPeerNotifySettings
        entity = await self._client.get_input_entity(chat_id)
        settings = InputPeerNotifySettings(mute_until=2**31 - 1 if mute else 0)
        await self._client(
            UpdateNotifySettingsRequest(peer=InputNotifyPeer(peer=entity), settings=settings)
        )
        return {"status": "muted" if mute else "unmuted"}

    async def leave_chat(self, chat_id: int | str) -> dict[str, str]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        entity = await self._client.get_entity(chat_id)
        if isinstance(entity, Channel):
            await self._client.delete_dialog(entity)
        elif isinstance(entity, Chat):
            await self._client.delete_dialog(entity)
        return {"status": "left"}

    async def delete_chat(self, chat_id: int | str) -> dict[str, str]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        entity = await self._client.get_entity(chat_id)
        await self._client.delete_dialog(entity)
        return {"status": "deleted"}

    async def mark_read(
        self, chat_id: int | str, topic_id: int | None = None,
    ) -> dict[str, Any]:
        """Mark a chat (and optionally a forum topic) as read.

        Forum supergroups maintain a separate per-topic read cursor that the
        chat-level send_read_acknowledge does NOT clear. Pass topic_id=1 for
        the General topic (e.g. GLC's "Lounge"), or the topic root msg_id for
        any other forum topic.
        """
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        entity = await self._client.get_entity(chat_id)

        await self._client.send_read_acknowledge(entity)
        result: dict[str, Any] = {"status": "marked_read"}

        if topic_id is not None:
            from telethon.tl.functions.messages import ReadDiscussionRequest
            peer = await self._client.get_input_entity(chat_id)
            latest = await self._client.get_messages(entity, limit=1)
            max_id = latest[0].id if latest else 0
            await self._client(ReadDiscussionRequest(
                peer=peer, msg_id=int(topic_id), read_max_id=max_id,
            ))
            result["topic_id"] = int(topic_id)
            result["read_max_id"] = max_id

        return result

    # --- Messages: Read ---

    async def read_messages(
        self, chat_id: int | str, limit: int = 20,
        offset_date: str | None = None, from_user: int | str | None = None,
        topic_id: int | None = None,
    ) -> list[dict[str, Any]]:
        self._rl_fetch.acquire()
        chat_id = validate_chat_id(chat_id)
        kwargs: dict[str, Any] = {"limit": min(limit, 100)}
        if offset_date:
            kwargs["offset_date"] = datetime.fromisoformat(offset_date)
        if from_user:
            kwargs["from_user"] = from_user
        if topic_id is not None:
            kwargs["reply_to"] = int(topic_id)

        messages = await self._client.get_messages(chat_id, **kwargs)
        result = [_msg_to_dict(m) for m in messages if isinstance(m, Message)]
        self._cache_messages(result)
        return [_fence_message(m) for m in result]

    async def search_messages(
        self, query: str, chat_id: int | str | None = None, limit: int = 20,
        chat_type: str | None = None,
    ) -> list[dict[str, Any]]:
        self._rl_search.acquire()
        kwargs: dict[str, Any] = {"limit": min(limit, 100)}
        cached_chat_id: int | None = None
        cached_chat_ids: list[int] | None = None

        if chat_type and chat_type not in ("user", "group", "channel"):
            raise ValueError(f"Invalid chat_type: {chat_type}. Must be user, group, or channel.")

        # When filtering by chat_type, search across matching chats
        if chat_type and chat_id is None:
            dialogs = await self._client.get_dialogs(limit=200)
            matching_entities = []
            for d in dialogs:
                dtype = "user"
                if isinstance(d.entity, Channel):
                    dtype = "channel" if d.entity.broadcast else "group"
                elif isinstance(d.entity, Chat):
                    dtype = "group"
                if dtype == chat_type:
                    matching_entities.append(d.entity)

            cached_chat_ids = [utils.get_peer_id(entity) for entity in matching_entities]
            live_results: list[dict[str, Any]] = []
            for ent in matching_entities:
                msgs = await self._client.get_messages(ent, search=query, **kwargs)
                live_results.extend(
                    _msg_to_dict(m) for m in msgs if isinstance(m, Message)
                )
            self._cache_messages(live_results)
        else:
            entity = None
            if chat_id is not None:
                chat_id = validate_chat_id(chat_id)
                entity = await self._client.get_entity(chat_id)
                # Telethon message.chat_id stores the marked peer ID, not the
                # bare Channel/Chat entity ID or the user's @username.
                cached_chat_id = utils.get_peer_id(entity)

            # Live search
            messages = await self._client.get_messages(entity, search=query, **kwargs)
            live_results = [_msg_to_dict(m) for m in messages if isinstance(m, Message)]
            self._cache_messages(live_results)

        # Merge with cache
        cache_results = self._cache.search(
            query, chat_id=cached_chat_id, chat_ids=cached_chat_ids, limit=limit
        )
        live_keys = {(m["chat_id"], m["id"]) for m in live_results}
        merged = live_results + [
            c for c in cache_results if (c["chat_id"], c["id"]) not in live_keys
        ]
        merged.sort(key=lambda m: m.get("date", ""), reverse=True)

        return [_fence_message(m) for m in merged[:limit]]

    async def search_regex(
        self, pattern: str, chat_id: int | str | None = None, limit: int = 20,
        after: str | None = None, before: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search cached messages using a regex pattern.

        The SQL fetch runs on the event loop (fast, bounded by the row cap);
        the regex scan runs in a worker thread with a per-row and total
        timeout so an expensive pattern can't stall the daemon.
        """
        import asyncio  # noqa: PLC0415

        resolved_chat_id = _cache_only_chat_id(chat_id)

        compiled = compile_search_pattern(pattern)
        rows = self._cache.regex_candidates(
            chat_id=resolved_chat_id, after=after, before=before,
        )
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, regex_filter, rows, compiled, limit)
        return [_fence_message(m) for m in results]

    async def get_message(self, chat_id: int | str, message_id: int) -> dict[str, Any]:
        self._rl_fetch.acquire()
        chat_id = validate_chat_id(chat_id)
        msg = await self._client.get_messages(chat_id, ids=message_id)
        if not msg:
            raise ValueError(f"Message {message_id} not found")
        result = _msg_to_dict(msg)
        self._cache_messages([result])
        return _fence_message(result)

    async def get_message_replies(
        self, chat_id: int | str, message_id: int, limit: int = 20,
    ) -> list[dict[str, Any]]:
        self._rl_fetch.acquire()
        chat_id = validate_chat_id(chat_id)
        messages = await self._client.get_messages(
            chat_id, reply_to=message_id, limit=min(limit, 100)
        )
        result = [_msg_to_dict(m) for m in messages if isinstance(m, Message)]
        self._cache_messages(result)
        return [_fence_message(m) for m in result]

    async def get_scheduled_messages(self, chat_id: int | str) -> list[dict[str, Any]]:
        self._rl_fetch.acquire()
        chat_id = validate_chat_id(chat_id)
        entity = await self._client.get_input_entity(chat_id)
        result = await self._client(GetScheduledHistoryRequest(peer=entity, hash=0))
        return [_fence_message(_msg_to_dict(m)) for m in result.messages if isinstance(m, Message)]

    # --- Messages: Write ---

    async def send_message(
        self, chat_id: int | str, text: str,
        reply_to: int | None = None,
        topic_id: int | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        """Send a message. Combine reply_to + topic_id to reply within a topic."""
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        validate_message_length(text)

        send_reply_to: int | None = reply_to
        if topic_id is not None and reply_to is None:
            send_reply_to = int(topic_id)
        elif reply_to is not None:
            send_reply_to = int(reply_to)

        msg = await self._client.send_message(
            chat_id, text, reply_to=send_reply_to, parse_mode=parse_mode,
        )
        return _msg_to_dict(msg)

    async def edit_message(
        self, chat_id: int | str, message_id: int, text: str,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        validate_message_length(text)
        msg = await self._client.edit_message(
            chat_id, message_id, text, parse_mode=parse_mode,
        )
        return _msg_to_dict(msg)

    async def delete_message(
        self, chat_id: int | str, message_ids: list[int],
    ) -> dict[str, str]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        await self._client.delete_messages(chat_id, message_ids)
        return {"status": "deleted", "count": str(len(message_ids))}

    async def forward_message(
        self, from_chat: int | str, message_ids: list[int], to_chat: int | str,
    ) -> dict[str, str]:
        self._rl_write.acquire()
        from_chat = validate_chat_id(from_chat)
        to_chat = validate_chat_id(to_chat)
        await self._client.forward_messages(to_chat, message_ids, from_chat)
        return {"status": "forwarded", "count": str(len(message_ids))}

    async def schedule_message(
        self, chat_id: int | str, text: str, schedule_date: str,
        reply_to: int | None = None,
    ) -> dict[str, Any]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        validate_message_length(text)
        dt = datetime.fromisoformat(schedule_date)
        msg = await self._client.send_message(chat_id, text, reply_to=reply_to, schedule=dt)
        return _msg_to_dict(msg)

    async def send_reaction(
        self, chat_id: int | str, message_id: int, emoji: str,
    ) -> dict[str, str]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        entity = await self._client.get_input_entity(chat_id)
        await self._client(SendReactionRequest(
            peer=entity, msg_id=message_id,
            reaction=[ReactionEmoji(emoticon=emoji)],
        ))
        return {"status": "reacted", "emoji": emoji}

    # --- Messages: Manage ---

    async def pin_message(self, chat_id: int | str, message_id: int) -> dict[str, str]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        await self._client.pin_message(chat_id, message_id)
        return {"status": "pinned"}

    async def unpin_message(self, chat_id: int | str, message_id: int) -> dict[str, str]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        await self._client.unpin_message(chat_id, message_id)
        return {"status": "unpinned"}

    # --- Media ---

    async def _download_to_dir(self, msg: Message, target_dir: str) -> dict[str, str]:
        """Download *msg*'s media into *target_dir* without clobbering anything.

        The destination is created by us with O_EXCL|O_NOFOLLOW (see
        create_unique_file) and handed to Telethon as an open file, so
        Telethon never picks the name, never overwrites an existing file,
        and never writes through a symlink. The original filename is
        attacker-controlled and is returned fenced.
        """
        original = getattr(getattr(msg, "file", None), "name", None)
        ext = getattr(getattr(msg, "file", None), "ext", None) or ""
        kind = "photo" if isinstance(msg.media, MessageMediaPhoto) else "file"
        wanted = original if isinstance(original, str) and original else f"{kind}_{msg.id}{ext}"
        if not os.path.splitext(wanted)[1] and ext:
            wanted += ext

        fd, final_path = create_unique_file(target_dir, wanted)
        try:
            with os.fdopen(fd, "wb") as fh:
                written = await self._client.download_media(msg, file=fh)
        except BaseException:
            _unlink_quietly(final_path)
            raise
        if written is None:
            _unlink_quietly(final_path)
            raise ValueError("Failed to download media")

        basename = os.path.basename(final_path)
        return {
            "dir": target_dir,
            "path": fence(final_path, "filename"),
            "filename": fence(basename, "filename"),
            "original_filename": fence(original, "filename"),
        }

    async def download_media(self, chat_id: int | str, message_id: int) -> dict[str, str]:
        self._rl_fetch.acquire()
        chat_id = validate_chat_id(chat_id)
        msg = await self._client.get_messages(chat_id, ids=message_id)
        if not msg or not msg.media:
            raise ValueError("Message has no media")
        return await self._download_to_dir(msg, DOWNLOADS_DIR)

    async def download_chat_media(
        self, chat_id: int | str, limit: int = 50,
        media_type: str = "photo", output_dir: str | None = None,
    ) -> dict[str, Any]:
        """Download media files from a chat in bulk."""
        self._rl_fetch.acquire()
        chat_id = validate_chat_id(chat_id)
        entity = await self._client.get_entity(chat_id)

        target_dir = output_dir or DOWNLOADS_DIR
        if output_dir and not is_path_allowed(output_dir, self._upload_dirs + [DOWNLOADS_DIR]):
            raise ValueError(
                f"Output directory not in allowed paths. Allowed: {DOWNLOADS_DIR}"
            )
        ensure_dir(target_dir)

        results: list[dict[str, Any]] = []
        downloaded = 0

        async for msg in self._client.iter_messages(entity, limit=min(limit, 200)):
            if not isinstance(msg, Message) or not msg.media:
                continue

            classified = None
            if isinstance(msg.media, MessageMediaPhoto):
                classified = "photo"
            elif isinstance(msg.media, MessageMediaDocument):
                classified = "document"
            else:
                continue

            if media_type != "all" and classified != media_type:
                continue

            try:
                info = await self._download_to_dir(msg, target_dir)
                results.append({"msg_id": msg.id, "media_type": classified, **info})
                downloaded += 1
            except Exception as e:
                results.append({
                    "msg_id": msg.id,
                    "media_type": classified,
                    "error": str(e),
                })

            if downloaded >= limit:
                break

        return {
            "chat_id": entity.id,
            "downloaded": downloaded,
            "files": results,
        }

    async def send_file(
        self, chat_id: int | str, file_path: str, caption: str = "",
        topic_id: int | None = None, reply_to: int | None = None,
    ) -> dict[str, Any]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        if not is_path_allowed(file_path, self._upload_dirs):
            raise ValueError(
                f"File not in allowed upload directories: {', '.join(self._upload_dirs)}"
            )
        if not os.path.exists(file_path):
            raise ValueError(f"File not found: {file_path}")
        send_reply_to: int | None = reply_to
        if topic_id is not None and reply_to is None:
            send_reply_to = int(topic_id)
        elif reply_to is not None:
            send_reply_to = int(reply_to)
        msg = await self._client.send_file(
            chat_id, file_path, caption=caption, reply_to=send_reply_to,
        )
        return _msg_to_dict(msg)

    async def send_voice(self, chat_id: int | str, file_path: str) -> dict[str, Any]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        if not is_path_allowed(file_path, self._upload_dirs):
            raise ValueError("File not in allowed upload directories")
        msg = await self._client.send_file(chat_id, file_path, voice_note=True)
        return _msg_to_dict(msg)

    async def send_location(self, chat_id: int | str, lat: float, lon: float) -> dict[str, str]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        from telethon.tl.types import InputGeoPoint
        await self._client.send_message(chat_id, file=InputGeoPoint(lat=lat, long=lon))
        return {"status": "sent", "lat": str(lat), "lon": str(lon)}

    async def get_sticker_sets(self) -> list[dict[str, Any]]:
        self._rl_fetch.acquire()
        from telethon.tl.functions.messages import GetAllStickersRequest
        result = await self._client(GetAllStickersRequest(hash=0))
        return [
            {"id": s.id, "title": fence(s.title, "title"), "count": s.count}
            for s in result.sets
        ]

    # --- Contacts ---

    async def list_contacts(self) -> list[dict[str, Any]]:
        self._rl_fetch.acquire()
        result = await self._client(GetContactsRequest(hash=0))
        return [{
            "id": u.id,
            "name": fence(f"{u.first_name or ''} {u.last_name or ''}".strip(), "sender"),
            "username": u.username,
            "phone": u.phone,
        } for u in result.users]

    async def get_contact(self, user_id: int | str) -> dict[str, Any]:
        self._rl_fetch.acquire()
        entity = await self._client.get_entity(user_id)
        if not isinstance(entity, User):
            raise ValueError("Not a user")
        return {
            "id": entity.id,
            "name": fence(
                f"{entity.first_name or ''} {entity.last_name or ''}".strip(), "sender"
            ),
            "username": entity.username,
            "phone": entity.phone,
            "bio": fence(getattr(entity, "about", None), "bio"),
        }

    # --- Users ---

    async def get_user(self, user_id: int | str) -> dict[str, Any]:
        return await self.get_contact(user_id)

    async def block_user(self, user_id: int | str) -> dict[str, str]:
        self._rl_write.acquire()
        entity = await self._client.get_input_entity(user_id)
        await self._client(BlockRequest(id=entity))
        return {"status": "blocked"}

    async def unblock_user(self, user_id: int | str) -> dict[str, str]:
        self._rl_write.acquire()
        entity = await self._client.get_input_entity(user_id)
        await self._client(UnblockRequest(id=entity))
        return {"status": "unblocked"}

    # --- Groups & Channels ---

    async def get_participants(
        self, chat_id: int | str, limit: int = 100,
    ) -> list[dict[str, Any]]:
        self._rl_fetch.acquire()
        chat_id = validate_chat_id(chat_id)
        entity = await self._client.get_input_entity(chat_id)
        result = await self._client(GetParticipantsRequest(
            channel=entity, filter=ChannelParticipantsSearch(""),
            offset=0, limit=min(limit, 200), hash=0,
        ))
        return [{
            "id": u.id,
            "name": fence(f"{u.first_name or ''} {u.last_name or ''}".strip(), "sender"),
            "username": u.username,
        } for u in result.users]

    async def add_participant(self, chat_id: int | str, user_id: int | str) -> dict[str, str]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        channel = await self._client.get_input_entity(chat_id)
        user = await self._client.get_input_entity(user_id)
        await self._client(InviteToChannelRequest(channel=channel, users=[user]))
        return {"status": "added"}

    async def remove_participant(
        self, chat_id: int | str, user_id: int | str,
    ) -> dict[str, str]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        channel = await self._client.get_input_entity(chat_id)
        user = await self._client.get_input_entity(user_id)
        rights = ChatBannedRights(until_date=None, view_messages=True)
        await self._client(EditBannedRequest(
            channel=channel, participant=user, banned_rights=rights
        ))
        return {"status": "removed"}

    async def set_chat_title(self, chat_id: int | str, title: str) -> dict[str, str]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        channel = await self._client.get_input_entity(chat_id)
        await self._client(EditTitleRequest(channel=channel, title=title))
        return {"status": "updated", "title": title}

    async def set_chat_description(
        self, chat_id: int | str, description: str,
    ) -> dict[str, str]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        entity = await self._client.get_entity(chat_id)
        if isinstance(entity, Channel):
            from telethon.tl.functions.channels import EditAboutRequest
            await self._client(EditAboutRequest(channel=entity, about=description))
        return {"status": "updated"}

    async def set_chat_photo(self, chat_id: int | str, file_path: str) -> dict[str, str]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        if not is_path_allowed(file_path, self._upload_dirs):
            raise ValueError("File not in allowed upload directories")
        entity = await self._client.get_entity(chat_id)
        photo = await self._client.upload_file(file_path)
        from telethon.tl.types import InputChatUploadedPhoto
        await self._client(EditPhotoRequest(
            channel=entity, photo=InputChatUploadedPhoto(file=photo)
        ))
        return {"status": "updated"}

    async def list_forum_topics(
        self, chat_id: int | str, limit: int = 100, query: str | None = None,
    ) -> list[dict[str, Any]]:
        """List topics in a forum supergroup.

        The General topic is always topic id 1 even though it has no
        explicit ForumTopic entry returned by the API.
        """
        self._rl_fetch.acquire()
        chat_id = validate_chat_id(chat_id)
        from telethon.tl.functions.messages import GetForumTopicsRequest
        peer = await self._client.get_input_entity(chat_id)
        result = await self._client(GetForumTopicsRequest(
            peer=peer,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=min(limit, 100),
            q=query,
        ))
        topics: list[dict[str, Any]] = []
        for t in result.topics:
            if not hasattr(t, "title"):
                continue  # ForumTopicDeleted has only id
            topics.append({
                "id": t.id,
                "title": fence(t.title, "title"),
                "top_message": t.top_message,
                "unread_count": t.unread_count,
                "closed": bool(getattr(t, "closed", False)),
                "pinned": bool(getattr(t, "pinned", False)),
                "hidden": bool(getattr(t, "hidden", False)),
            })
        return topics

    async def get_invite_link(self, chat_id: int | str) -> dict[str, str]:
        self._rl_fetch.acquire()
        chat_id = validate_chat_id(chat_id)
        channel = await self._client.get_input_entity(chat_id)
        result = await self._client(ExportChatInviteRequest(peer=channel))
        return {"link": result.link}

    async def get_admin_log(
        self, chat_id: int | str, limit: int = 50,
    ) -> list[dict[str, Any]]:
        self._rl_fetch.acquire()
        chat_id = validate_chat_id(chat_id)
        channel = await self._client.get_input_entity(chat_id)
        result = await self._client(GetAdminLogRequest(
            channel=channel, q="", max_id=0, min_id=0, limit=min(limit, 100),
        ))
        return [{
            "id": e.id,
            "date": e.date.isoformat() if e.date else "",
            "user_id": e.user_id,
            "action": type(e.action).__name__,
        } for e in result.events]

    # --- Account & Utility ---

    async def get_me(self) -> dict[str, Any]:
        me = await self._client.get_me()
        return {
            "id": me.id,
            "name": f"{me.first_name or ''} {me.last_name or ''}".strip(),
            "username": me.username,
            "phone": me.phone,
        }

    async def resolve_peer_id(self, peer: int | str) -> int:
        """Resolve *peer* exactly as a send would, to Telethon's marked peer id.

        Marked ids are unambiguous across users (positive), small groups
        (negative) and channels (``-100`` prefixed), unlike the bare ids the
        tools accept. Raises if Telethon cannot resolve the peer.
        """
        from telethon.utils import get_peer_id  # noqa: PLC0415

        entity = await self._client.get_input_entity(validate_chat_id(peer))
        return get_peer_id(entity)

    async def get_status(self) -> dict[str, Any]:
        connected = self._client.is_connected()
        authorized = await self._client.is_user_authorized() if connected else False
        return {
            "connected": connected,
            "authorized": authorized,
            "daemon_pid": os.getpid(),
            **_package_info(),
        }

    async def get_dialogs_stats(self) -> dict[str, Any]:
        self._rl_fetch.acquire()
        dialogs = await self._client.get_dialogs(limit=100)
        total_unread = sum(d.unread_count for d in dialogs)
        return {
            "total_chats": len(dialogs),
            "total_unread": total_unread,
            "chats_with_unread": len([d for d in dialogs if d.unread_count > 0]),
        }

    async def export_chat(
        self, chat_id: int | str, limit: int = 1000,
    ) -> dict[str, Any]:
        """Export a chat live from Telegram to a JSON file in the downloads dir.

        The raw messages go to disk for the user; the model gets the path
        and a fenced summary, never the unfenced bodies.
        """
        self._rl_fetch.acquire()
        chat_id = validate_chat_id(chat_id)
        limit = min(limit, 1000)  # Hard cap
        messages = await self._client.get_messages(chat_id, limit=limit)
        result = [_msg_to_dict(m) for m in messages if isinstance(m, Message)]
        self._cache_messages(result)

        ensure_dir(DOWNLOADS_DIR)
        fd, path = create_unique_file(DOWNLOADS_DIR, _export_filename("export_chat", "json"))
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(result, indent=2, default=str))
        return _export_summary(result, path, "json")

    async def clear_cache(self) -> dict[str, str]:
        self._cache.clear()
        return {"status": "cache_cleared"}

    async def sync_chat(self, chat_id: int | str, limit: int = 1000) -> dict[str, Any]:
        """Sync messages from a single chat into the local cache."""
        self._rl_fetch.acquire()
        chat_id = validate_chat_id(chat_id)
        entity = await self._client.get_entity(chat_id)

        chat_name = ""
        chat_type = "user"
        if isinstance(entity, User):
            chat_name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()
        elif isinstance(entity, (Chat, Channel)):
            chat_name = entity.title or ""
            chat_type = "channel" if (isinstance(entity, Channel) and entity.broadcast) else "group"

        min_id = self._cache.get_last_msg_id(entity.id) or 0
        limit = min(limit, 5000)

        batch: list[dict[str, Any]] = []
        total_fetched = 0
        BATCH_SIZE = 200

        async for msg in self._client.iter_messages(entity, limit=limit, min_id=min_id):
            if not isinstance(msg, Message):
                continue
            if msg.text is None and msg.message is None:
                continue
            batch.append(_msg_to_dict(msg))
            total_fetched += 1

            if len(batch) >= BATCH_SIZE:
                self._cache.insert_batch(batch)
                batch.clear()

        if batch:
            self._cache.insert_batch(batch)

        self._cache.cache_chat(entity.id, chat_name, chat_type)
        return {
            "chat_id": entity.id,
            "chat_name": fence(chat_name, "title"),
            "messages_synced": total_fetched,
        }

    async def sync_messages(
        self, chat_id: int | str | None = None, limit: int = 1000,
        max_chats: int = 50,
    ) -> dict[str, Any]:
        """Sync messages from all chats (or a specific chat) into the local cache."""
        if chat_id:
            return await self.sync_chat(chat_id, limit=limit)

        self._rl_fetch.acquire()
        dialogs = await self._client.get_dialogs(limit=max_chats)
        results: list[dict[str, Any]] = []
        total_messages = 0

        for d in dialogs:
            chat_type = "user"
            if isinstance(d.entity, Channel):
                chat_type = "channel" if d.entity.broadcast else "group"
            elif isinstance(d.entity, Chat):
                chat_type = "group"

            min_id = self._cache.get_last_msg_id(d.entity.id) or 0
            batch: list[dict[str, Any]] = []
            chat_count = 0

            try:
                async for msg in self._client.iter_messages(
                    d.entity, limit=min(limit, 1000), min_id=min_id
                ):
                    if not isinstance(msg, Message):
                        continue
                    if msg.text is None and msg.message is None:
                        continue
                    batch.append(_msg_to_dict(msg))
                    chat_count += 1

                    if len(batch) >= 200:
                        self._cache.insert_batch(batch)
                        batch.clear()
            except Exception as e:
                logger.warning("Error syncing chat %s: %s", d.name, e)

            if batch:
                self._cache.insert_batch(batch)

            self._cache.cache_chat(d.entity.id, d.name, chat_type)
            total_messages += chat_count
            results.append({
                "chat_id": d.entity.id,
                "chat_name": fence(d.name, "title"),
                "messages_synced": chat_count,
            })

        return {
            "chats_synced": len(results),
            "total_messages": total_messages,
            "details": results,
        }

    async def message_timeline(
        self, chat_id: int | str | None = None, granularity: str = "day",
        after: str | None = None, before: str | None = None,
    ) -> dict[str, Any]:
        """Return message counts grouped by time period from the cache."""
        resolved_chat_id = _cache_only_chat_id(chat_id)

        entries = self._cache.timeline(
            chat_id=resolved_chat_id, granularity=granularity,
            after=after, before=before,
        )
        return {"granularity": granularity, "timeline": entries}

    async def today_messages(
        self, chat_id: int | str | None = None, limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return today's messages from the cache."""
        resolved_chat_id = _cache_only_chat_id(chat_id)

        results = self._cache.get_today(chat_id=resolved_chat_id, limit=limit)
        return [_fence_message(m) for m in results]

    async def get_new_messages(
        self, since: str, chat_id: int | str | None = None, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get messages newer than *since* (ISO datetime). Optionally scoped to a chat."""
        self._rl_fetch.acquire()
        since_dt = datetime.fromisoformat(since)
        limit = min(limit, 200)

        if chat_id:
            chat_id = validate_chat_id(chat_id)
            messages = await self._client.get_messages(
                chat_id, limit=limit, offset_date=None,
            )
            results = [
                _msg_to_dict(m)
                for m in messages
                if isinstance(m, Message) and m.date and m.date >= since_dt
            ]
        else:
            dialogs = await self._client.get_dialogs(limit=50)
            results: list[dict[str, Any]] = []
            for d in dialogs:
                if d.date and d.date < since_dt:
                    continue
                msgs = await self._client.get_messages(d.entity, limit=min(limit, 10))
                for m in msgs:
                    if isinstance(m, Message) and m.date and m.date >= since_dt:
                        results.append(_msg_to_dict(m))

        self._cache_messages(results)
        results.sort(key=lambda m: m.get("date", ""), reverse=True)
        return [_fence_message(m) for m in results[:limit]]

    async def chat_analytics(
        self, chat_id: int | str | None = None, limit: int = 20,
        after: str | None = None, before: str | None = None,
    ) -> dict[str, Any]:
        """Return analytics for a chat: top senders and message counts from cache."""
        resolved_chat_id = _cache_only_chat_id(chat_id)

        top = self._cache.top_senders(
            chat_id=resolved_chat_id, limit=limit, after=after, before=before,
        )
        return {
            "top_senders": [
                {"sender_name": fence(s["sender_name"], "sender"), "msg_count": s["msg_count"]}
                for s in top
            ],
            "total_senders": len(top),
        }

    async def export_cached_messages(
        self, chat_id: int | str | None = None, limit: int = 5000,
        after: str | None = None, before: str | None = None,
        format: str = "json",
    ) -> dict[str, Any]:
        """Export cached messages as a JSON or CSV file in the downloads dir.

        Like export_chat, the raw rows go to disk and the model gets the
        path plus a fenced summary.
        """
        if format not in ("json", "csv"):
            raise ValueError(f"Invalid format: {format}. Must be json or csv.")

        resolved_chat_id = _cache_only_chat_id(chat_id)

        messages = self._cache.export_messages(
            chat_id=resolved_chat_id, limit=limit, after=after, before=before,
        )

        if format == "csv":
            import csv  # noqa: PLC0415
            import io  # noqa: PLC0415
            output = io.StringIO()
            if messages:
                writer = csv.DictWriter(output, fieldnames=messages[0].keys())
                writer.writeheader()
                writer.writerows(messages)
            data = output.getvalue()
        else:
            data = json.dumps(messages, indent=2, default=str)

        ensure_dir(DOWNLOADS_DIR)
        fd, path = create_unique_file(DOWNLOADS_DIR, _export_filename("export_cached", format))
        with os.fdopen(fd, "w") as f:
            f.write(data)
        return _export_summary(messages, path, format)
