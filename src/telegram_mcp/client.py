"""Telethon wrapper — all Telegram API calls."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from telethon import TelegramClient
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

from telegram_mcp.cache import MessageCache
from telegram_mcp.login import CONFIG_DIR, DOWNLOADS_DIR, SESSION_PATH, load_config
from telegram_mcp.security import (
    RateLimiter,
    ensure_dir,
    fence,
    is_path_allowed,
    sanitize_filename,
    validate_chat_id,
    validate_message_length,
)


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

    return {
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


def _fence_message(msg_dict: dict[str, Any]) -> dict[str, Any]:
    """Apply content fencing to a message dict."""
    return {
        **msg_dict,
        "text": fence(msg_dict.get("text"), "message"),
        "sender_name": fence(msg_dict.get("sender_name"), "sender"),
    }


class TelegramMCPClient:
    """High-level wrapper around Telethon for MCP tool use."""

    def __init__(self):
        config = load_config()
        self._api_id = config.get("api_id")
        self._api_hash = config.get("api_hash")
        if not self._api_id or not self._api_hash:
            raise RuntimeError("Not configured. Run 'telegram-mcp login' first.")

        self._client = TelegramClient(SESSION_PATH, self._api_id, self._api_hash)
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

        ensure_dir(DOWNLOADS_DIR)

    async def connect(self) -> None:
        """Connect to Telegram."""
        if not self._connected:
            await self._client.connect()
            if not await self._client.is_user_authorized():
                raise RuntimeError("Not authorized. Run 'telegram-mcp login' first.")
            self._connected = True

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

    async def mark_read(self, chat_id: int | str) -> dict[str, str]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        entity = await self._client.get_entity(chat_id)
        await self._client.send_read_acknowledge(entity)
        return {"status": "marked_read"}

    # --- Messages: Read ---

    async def read_messages(
        self, chat_id: int | str, limit: int = 20,
        offset_date: str | None = None, from_user: int | str | None = None,
    ) -> list[dict[str, Any]]:
        self._rl_fetch.acquire()
        chat_id = validate_chat_id(chat_id)
        kwargs: dict[str, Any] = {"limit": min(limit, 100)}
        if offset_date:
            kwargs["offset_date"] = datetime.fromisoformat(offset_date)
        if from_user:
            kwargs["from_user"] = from_user

        messages = await self._client.get_messages(chat_id, **kwargs)
        result = [_msg_to_dict(m) for m in messages if isinstance(m, Message)]
        self._cache_messages(result)
        return [_fence_message(m) for m in result]

    async def search_messages(
        self, query: str, chat_id: int | str | None = None, limit: int = 20,
    ) -> list[dict[str, Any]]:
        self._rl_search.acquire()
        kwargs: dict[str, Any] = {"limit": min(limit, 100)}
        entity = None
        if chat_id:
            chat_id = validate_chat_id(chat_id)
            entity = await self._client.get_entity(chat_id)

        # Live search
        messages = await self._client.get_messages(entity, search=query, **kwargs)
        live_results = [_msg_to_dict(m) for m in messages if isinstance(m, Message)]
        self._cache_messages(live_results)

        # Merge with cache
        cache_results = self._cache.search(
            query, chat_id=chat_id if isinstance(chat_id, int) else None, limit=limit
        )
        live_ids = {m["id"] for m in live_results}
        merged = live_results + [c for c in cache_results if c["id"] not in live_ids]
        merged.sort(key=lambda m: m.get("date", ""), reverse=True)

        return [_fence_message(m) for m in merged[:limit]]

    async def get_message(self, chat_id: int | str, message_id: int) -> dict[str, Any]:
        self._rl_fetch.acquire()
        chat_id = validate_chat_id(chat_id)
        msgs = await self._client.get_messages(chat_id, ids=message_id)
        if not msgs or not msgs[0]:
            raise ValueError(f"Message {message_id} not found")
        result = _msg_to_dict(msgs[0])
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
        return [_msg_to_dict(m) for m in result.messages if isinstance(m, Message)]

    # --- Messages: Write ---

    async def send_message(
        self, chat_id: int | str, text: str,
        reply_to: int | None = None,
    ) -> dict[str, Any]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        validate_message_length(text)
        msg = await self._client.send_message(chat_id, text, reply_to=reply_to)
        return _msg_to_dict(msg)

    async def edit_message(
        self, chat_id: int | str, message_id: int, text: str,
    ) -> dict[str, Any]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        validate_message_length(text)
        msg = await self._client.edit_message(chat_id, message_id, text)
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

    async def download_media(self, chat_id: int | str, message_id: int) -> dict[str, str]:
        self._rl_fetch.acquire()
        chat_id = validate_chat_id(chat_id)
        msgs = await self._client.get_messages(chat_id, ids=message_id)
        if not msgs or not msgs[0] or not msgs[0].media:
            raise ValueError("Message has no media")
        path = await self._client.download_media(msgs[0], file=DOWNLOADS_DIR)
        if path:
            # Sanitize the downloaded filename
            basename = sanitize_filename(os.path.basename(path))
            final_path = os.path.join(DOWNLOADS_DIR, basename)
            if path != final_path:
                os.rename(path, final_path)
            return {"path": final_path, "filename": basename}
        raise ValueError("Failed to download media")

    async def send_file(
        self, chat_id: int | str, file_path: str, caption: str = "",
    ) -> dict[str, Any]:
        self._rl_write.acquire()
        chat_id = validate_chat_id(chat_id)
        if not is_path_allowed(file_path, self._upload_dirs):
            raise ValueError(
                f"File not in allowed upload directories: {', '.join(self._upload_dirs)}"
            )
        if not os.path.exists(file_path):
            raise ValueError(f"File not found: {file_path}")
        msg = await self._client.send_file(chat_id, file_path, caption=caption)
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

    async def get_status(self) -> dict[str, Any]:
        connected = self._client.is_connected()
        authorized = await self._client.is_user_authorized() if connected else False
        return {"connected": connected, "authorized": authorized}

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
    ) -> list[dict[str, Any]]:
        self._rl_fetch.acquire()
        chat_id = validate_chat_id(chat_id)
        limit = min(limit, 1000)  # Hard cap
        messages = await self._client.get_messages(chat_id, limit=limit)
        result = [_msg_to_dict(m) for m in messages if isinstance(m, Message)]
        self._cache_messages(result)
        return result  # Unfenced — export is for the user's own data

    async def clear_cache(self) -> dict[str, str]:
        self._cache.clear()
        return {"status": "cache_cleared"}
