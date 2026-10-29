"""Telegram delivery: send files or cached file_ids, return the file_ids Telegram assigned.

Workers call `TelegramSender`; tests use `FakeSender` which records calls instead.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Protocol

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.types import (
    FSInputFile,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    ReplyParameters,
)

from core.logging import get_logger

log = get_logger("telegram")

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
        self,
        chat_id: int,
        files: list[Path],
        caption: str | None,
        *,
        reply_to: int | None = None,
        thread_id: int | None = None,
    ) -> list[str]: ...

    async def send_cached(
        self,
        chat_id: int,
        file_ids: list[str],
        caption: str | None,
        *,
        reply_to: int | None = None,
        thread_id: int | None = None,
    ) -> None: ...

    async def finish(self, chat_id: int, message_id: int, text: str | None) -> None: ...


class DeliveryError(Exception):
    """The file was produced but could not be handed to Telegram (slow link, network).

    Never retry an upload automatically after this: `request_timeout` also covers waiting
    for Telegram's reply, so a body that finished uploading can still be delivered by the
    server after the client gave up, and a retry would post the same file twice. There is
    no Bot API call to check whether the first one landed.
    """


class ForbiddenError(DeliveryError):
    """Telegram refused the send for good: bot kicked, or no right to post media here."""


_RIGHTS_RE = re.compile(
    r"not enough rights|have no rights|CHAT_SEND_\w+_FORBIDDEN|CHAT_WRITE_FORBIDDEN|"
    r"bot was kicked|bot is not a member|chat not found|TOPIC_CLOSED",
    re.IGNORECASE,
)


def is_rights_error(e: Exception) -> bool:
    return isinstance(e, TelegramForbiddenError) or bool(_RIGHTS_RE.search(str(e)))


def _reply_kwargs(reply_to: int | None, thread_id: int | None) -> dict:
    kw: dict = {}
    if reply_to:
        # allow_sending_without_reply: the original may have been deleted meanwhile.
        kw["reply_parameters"] = ReplyParameters(
            message_id=reply_to, allow_sending_without_reply=True
        )
    if thread_id:
        kw["message_thread_id"] = thread_id
    return kw


def upload_timeout_for(
    size_bytes: int, seconds_per_mb: float, cap_s: int, floor_s: int = 60
) -> int:
    """Request timeout for one upload: aiogram's 60 s default is too short for a 48 MB file
    on a slow link (0.4 MB/s = 2 min), so scale with size, between a floor and a cap."""
    mb = size_bytes / (1024 * 1024)
    return int(min(cap_s, max(floor_s, mb * seconds_per_mb)))


def _sizes(files: list[Path]) -> list[int]:
    return [f.stat().st_size for f in files]


class TelegramSender:
    def __init__(
        self, bot: Bot, upload_timeout: int = 600, upload_seconds_per_mb: float = 8.0
    ) -> None:
        self.bot = bot
        self.upload_timeout = upload_timeout
        self.upload_seconds_per_mb = upload_seconds_per_mb

    def _timeout(self, size_bytes: int) -> int:
        return upload_timeout_for(size_bytes, self.upload_seconds_per_mb, self.upload_timeout)

    async def send_files(
        self,
        chat_id: int,
        files: list[Path],
        caption: str | None,
        *,
        reply_to: int | None = None,
        thread_id: int | None = None,
    ) -> list[str]:
        files = [f for f in files if f.is_file()][:MEDIA_GROUP_MAX]
        if not files:
            raise RuntimeError("nothing to send")
        kinds = [_kind(f) for f in files]
        sizes = _sizes(files)
        total = sum(sizes)
        where = _reply_kwargs(reply_to, thread_id)
        started = time.monotonic()
        try:
            if len(files) > 1 and all(k in {"photo", "video"} for k in kinds):
                ids = await self._send_group(chat_id, files, kinds, caption, total, where)
            else:
                ids = []
                for i, (f, k, size) in enumerate(zip(files, kinds, sizes, strict=True)):
                    cap = caption if i == 0 else None
                    ids.append(_file_id(await self._send_one(chat_id, f, k, cap, size, where)))
        except (TelegramNetworkError, TimeoutError) as e:
            took = time.monotonic() - started
            raise DeliveryError(f"{e.__class__.__name__}: {e} after {took:.0f}s, {total} B") from e
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            if is_rights_error(e):
                raise ForbiddenError(str(e)) from e
            raise
        took = time.monotonic() - started
        log.info(
            "upload_done",
            files=len(files),
            bytes=total,
            took_s=round(took, 1),
            mb_per_s=round(total / (1024 * 1024) / took, 2) if took else None,
        )
        return ids

    async def _send_group(
        self,
        chat_id: int,
        files: list[Path],
        kinds: list[str],
        caption: str | None,
        total: int,
        where: dict,
    ) -> list[str]:
        media = []
        for i, (f, k) in enumerate(zip(files, kinds, strict=True)):
            cap = caption if i == 0 else None
            inp = FSInputFile(f)
            media.append(
                InputMediaPhoto(media=inp, caption=cap)
                if k == "photo"
                else InputMediaVideo(media=inp, caption=cap, supports_streaming=True)
            )
        sent = await self.bot.send_media_group(
            chat_id, media=media, request_timeout=self._timeout(total), **where
        )
        return [_file_id(m) for m in sent]

    async def _send_one(
        self, chat_id: int, f: Path, kind: str, caption: str | None, size: int, where: dict
    ) -> Message:
        inp = FSInputFile(f)
        timeout = self._timeout(size)
        try:
            if kind == "photo":
                return await self.bot.send_photo(
                    chat_id, inp, caption=caption, request_timeout=timeout, **where
                )
            if kind == "video":
                return await self.bot.send_video(
                    chat_id,
                    inp,
                    caption=caption,
                    supports_streaming=True,
                    request_timeout=timeout,
                    **where,
                )
            if kind == "audio":
                return await self.bot.send_audio(
                    chat_id, inp, caption=caption, request_timeout=timeout, **where
                )
        except TelegramBadRequest as e:
            if is_rights_error(e):
                raise
            # e.g. unsupported codec / dimensions -> fall through to document
        return await self.bot.send_document(
            chat_id, inp, caption=caption, request_timeout=timeout, **where
        )

    async def send_cached(
        self,
        chat_id: int,
        file_ids: list[str],
        caption: str | None,
        *,
        reply_to: int | None = None,
        thread_id: int | None = None,
    ) -> None:
        # file_ids are opaque; send_document accepts any kind and Telegram keeps the type.
        where = _reply_kwargs(reply_to, thread_id)
        try:
            for i, fid in enumerate(file_ids[:MEDIA_GROUP_MAX]):
                await self.bot.send_document(
                    chat_id, fid, caption=caption if i == 0 else None, **where
                )
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            if is_rights_error(e):
                raise ForbiddenError(str(e)) from e
            raise

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
        self.targets: list[tuple[int | None, int | None]] = []  # (reply_to, thread_id) per send
        self._counter = 0

    async def send_files(
        self,
        chat_id: int,
        files: list[Path],
        caption: str | None,
        *,
        reply_to: int | None = None,
        thread_id: int | None = None,
    ) -> list[str]:
        self.sent_files.append((chat_id, list(files), caption))
        self.targets.append((reply_to, thread_id))
        ids = []
        for _ in files:
            self._counter += 1
            ids.append(f"fake-file-id-{self._counter}")
        return ids

    async def send_cached(
        self,
        chat_id: int,
        file_ids: list[str],
        caption: str | None,
        *,
        reply_to: int | None = None,
        thread_id: int | None = None,
    ) -> None:
        self.sent_cached.append((chat_id, list(file_ids), caption))
        self.targets.append((reply_to, thread_id))

    async def finish(self, chat_id: int, message_id: int, text: str | None) -> None:
        self.finished.append((chat_id, message_id, text))
