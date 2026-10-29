"""Telegram delivery: send files or cached file_ids, return the file_ids Telegram assigned.

Workers call `TelegramSender`; tests use `FakeSender` which records calls instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo, Message

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
AUDIO_EXT = {".m4a", ".mp3", ".opus", ".ogg", ".aac"}
PHOTO_MAX_BYTES = 10 * 1024 * 1024  # Telegram compresses photos; above this send as document
MEDIA_GROUP_MAX = 10


def make_bot(token: str, api_url: str | None = None, proxy: str | None = None) -> Bot:
    """Bot client. `api_url` points at a self-hosted Bot API server; `proxy` (http:// or
    socks5://) routes Bot API traffic through a proxy for networks where Telegram is blocked."""
    kwargs: dict = {}
    if api_url:
        kwargs["api"] = TelegramAPIServer.from_base(api_url.rstrip("/"))
    if proxy:
        kwargs["proxy"] = proxy
    session = AiohttpSession(**kwargs) if kwargs else None
    return Bot(token=token, session=session, default=DefaultBotProperties(parse_mode="HTML"))


def _kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        return "photo" if path.stat().st_size <= PHOTO_MAX_BYTES else "document"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    return "document"


def _file_id(msg: Message) -> str:
    for attr in ("video", "photo", "audio", "document", "animation"):
        obj = getattr(msg, attr, None)
        if obj:
            return obj[-1].file_id if isinstance(obj, list) else obj.file_id
    raise RuntimeError("sent message carries no file")


class Sender(Protocol):
    async def send_files(
        self, chat_id: int, files: list[Path], caption: str | None
    ) -> list[str]: ...

    async def send_cached(self, chat_id: int, file_ids: list[str], caption: str | None) -> None: ...

    async def finish(self, chat_id: int, message_id: int, text: str | None) -> None: ...


class TelegramSender:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send_files(self, chat_id: int, files: list[Path], caption: str | None) -> list[str]:
        files = [f for f in files if f.is_file()][:MEDIA_GROUP_MAX]
        if not files:
            raise RuntimeError("nothing to send")
        kinds = [_kind(f) for f in files]
        if len(files) > 1 and all(k in {"photo", "video"} for k in kinds):
            media = []
            for i, (f, k) in enumerate(zip(files, kinds, strict=True)):
                cap = caption if i == 0 else None
                inp = FSInputFile(f)
                media.append(
                    InputMediaPhoto(media=inp, caption=cap)
                    if k == "photo"
                    else InputMediaVideo(media=inp, caption=cap, supports_streaming=True)
                )
            sent = await self.bot.send_media_group(chat_id, media=media)
            return [_file_id(m) for m in sent]

        ids: list[str] = []
        for i, (f, k) in enumerate(zip(files, kinds, strict=True)):
            cap = caption if i == 0 else None
            ids.append(_file_id(await self._send_one(chat_id, f, k, cap)))
        return ids

    async def _send_one(self, chat_id: int, f: Path, kind: str, caption: str | None) -> Message:
        inp = FSInputFile(f)
        try:
            if kind == "photo":
                return await self.bot.send_photo(chat_id, inp, caption=caption)
            if kind == "video":
                return await self.bot.send_video(
                    chat_id, inp, caption=caption, supports_streaming=True
                )
            if kind == "audio":
                return await self.bot.send_audio(chat_id, inp, caption=caption)
        except TelegramBadRequest:
            pass  # e.g. unsupported codec / dimensions -> fall through to document
        return await self.bot.send_document(chat_id, inp, caption=caption)

    async def send_cached(self, chat_id: int, file_ids: list[str], caption: str | None) -> None:
        # file_ids are opaque; send_document accepts any kind and Telegram keeps the type.
        for i, fid in enumerate(file_ids[:MEDIA_GROUP_MAX]):
            await self.bot.send_document(chat_id, fid, caption=caption if i == 0 else None)

    async def finish(self, chat_id: int, message_id: int, text: str | None) -> None:
        try:
            if text is None:
                await self.bot.delete_message(chat_id, message_id)
            else:
                await self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
        except TelegramBadRequest:
            pass  # message already gone or unchanged; not worth failing the job


class FakeSender:
    """Records calls; used by tests."""

    def __init__(self) -> None:
        self.sent_files: list[tuple[int, list[Path], str | None]] = []
        self.sent_cached: list[tuple[int, list[str], str | None]] = []
        self.finished: list[tuple[int, int, str | None]] = []
        self._counter = 0

    async def send_files(self, chat_id: int, files: list[Path], caption: str | None) -> list[str]:
        self.sent_files.append((chat_id, list(files), caption))
        ids = []
        for _ in files:
            self._counter += 1
            ids.append(f"fake-file-id-{self._counter}")
        return ids

    async def send_cached(self, chat_id: int, file_ids: list[str], caption: str | None) -> None:
        self.sent_cached.append((chat_id, list(file_ids), caption))

    async def finish(self, chat_id: int, message_id: int, text: str | None) -> None:
        self.finished.append((chat_id, message_id, text))
