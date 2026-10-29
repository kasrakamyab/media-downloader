"""Telegram handlers. The gateway never downloads: detect -> availability -> limits ->
placeholder -> (quality keyboard for YouTube) -> enqueue."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from arq.connections import ArqRedis
from pydantic import BaseModel

from core.breaker import Breaker
from core.cache import FileCache, url_hash
from core.contracts import (
    JOB_FUNCTION,
    PROBE_FUNCTION,
    QUEUE_NAMES,
    FormatOption,
    Job,
    Platform,
    ProbeResult,
    Quality,
)
from core.detector import UnsupportedURL, detect, find_urls
from core.health import is_alive, stats
from core.limits import UserLimits
from core.logging import get_logger
from core.settings import Settings

log = get_logger("gateway")
router = Router()

PLATFORM_LABEL = {
    Platform.YOUTUBE: "YouTube",
    Platform.INSTAGRAM: "Instagram",
    Platform.PINTEREST: "Pinterest",
}
# Platforms whose worker answers probes; the others have one quality and skip the keyboard.
PROBE_PLATFORMS = {Platform.YOUTUBE}

WORKING = "⏳ Working on it…"
HELP = (
    "Send me a <b>YouTube</b>, <b>Instagram</b> or <b>Pinterest</b> link and I'll reply with "
    "the video or pictures.\n\n"
    "• YouTube: videos and Shorts; pick a quality from the menu, or I choose the best one "
    "under the size limit after {choice}s\n"
    "• Instagram: public posts, reels and carousels (up to 10 items)\n"
    "• Pinterest: pins and short boards (up to 10 items)\n\n"
    "Add <code>audio</code> after a YouTube link to skip the menu and get just the sound.\n\n"
    "Limits: {active} downloads at a time, {hourly} per hour.\n"
    "For personal use only. Private content is refused."
)


class PendingChoice(BaseModel):
    """A job waiting for the user to pick a quality (or for the timeout to pick for them)."""

    job: Job
    default: Quality
    offered: list[Quality]


def pending_key(job_id: str) -> str:
    return f"pending:{job_id}"


def probe_key(url: str) -> str:
    return f"probe:{url_hash(url)}"


async def platform_available(arq: ArqRedis, breaker: Breaker, platform: Platform) -> bool:
    return await is_alive(arq, platform) and not await breaker.is_open(platform)


def unavailable_text(platform: Platform, others_up: list[Platform]) -> str:
    text = f"{PLATFORM_LABEL[platform]} is temporarily unavailable."
    if others_up:
        names = [PLATFORM_LABEL[p] for p in others_up]
        verb = "works" if len(names) == 1 else "work"
        text += f" {' and '.join(names)} still {verb}."
    return text


def _help(settings: Settings) -> str:
    return HELP.format(
        active=settings.max_active_jobs,
        hourly=settings.hourly_limit,
        choice=settings.quality_choice_timeout,
    )


@router.message(CommandStart())
async def on_start(message: Message, settings: Settings) -> None:
    await message.answer("👋 Hi! " + _help(settings))


@router.message(Command("help"))
async def on_help(message: Message, settings: Settings) -> None:
    await message.answer(_help(settings))


@router.message(Command("stats"))
async def on_stats(message: Message, settings: Settings, arq: ArqRedis, breaker: Breaker) -> None:
    if not message.from_user or message.from_user.id not in settings.admin_id_set:
        return
    lines = []
    for platform in Platform:
        s = await stats(arq, platform)
        alive = await is_alive(arq, platform)
        open_ = await breaker.is_open(platform)
        depth = await arq.zcard(QUEUE_NAMES[platform])
        ok, err = s.get("ok", 0), s.get("error", 0)
        hits = s.get("cache_hit", 0)
        rate = f"{hits / ok:.0%}" if ok else "–"
        if open_:
            opened = await breaker.opened_at(platform) or time.time()
            state = f"🟠 breaker open {int((time.time() - opened) / 60)} min"
        elif alive:
            state = f"🟢 up, {await breaker.failures(platform)} recent failures"
        else:
            state = "🔴 no heartbeat"
        lines.append(
            f"<b>{PLATFORM_LABEL[platform]}</b> — {state}\n"
            f"   queue={depth} ok={ok} err={err} cache hits={rate}"
        )
    await message.answer("\n".join(lines))


@router.message(F.text)
async def on_text(
    message: Message,
    settings: Settings,
    arq: ArqRedis,
    limits: UserLimits,
    cache: FileCache,
    breaker: Breaker,
) -> None:
    if not message.from_user:
        return
    urls = find_urls(message.text or "")
    if not urls:
        await message.answer("Send me a link to a YouTube video, Instagram post or Pinterest pin.")
        return

    try:
        found = detect(urls[0])
    except UnsupportedURL:
        await message.answer(
            "I can't handle that link yet. I support YouTube, Instagram and Pinterest."
        )
        return

    quality = Quality.BEST
    if found.platform == Platform.YOUTUBE and "audio" in (message.text or "").lower().split():
        quality = Quality.AUDIO

    if await cache.is_blocked(found.url):
        await message.answer("That link has been blocked on this bot.")
        return

    if not await platform_available(arq, breaker, found.platform):
        others = [
            p for p in Platform if p != found.platform and await platform_available(arq, breaker, p)
        ]
        await message.answer(unavailable_text(found.platform, others))
        return

    reason = await limits.try_acquire(message.from_user.id)
    if reason == "hourly":
        await message.answer(
            f"You've hit the limit of {settings.hourly_limit} requests per hour. "
            "Try again a bit later."
        )
        return
    if reason == "active":
        await message.answer(
            f"You already have {settings.max_active_jobs} downloads running. "
            "Wait for them to finish first."
        )
        return

    placeholder = await message.reply(WORKING)
    job = Job(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        message_id=placeholder.message_id,
        url=found.url,
        platform=found.platform,
        quality=quality,
    )

    # A cached answer for the default quality skips the menu: the worker serves it instantly.
    if (
        found.platform in PROBE_PLATFORMS
        and quality == Quality.BEST
        and not await cache.get(found.url, Quality.BEST)
    ):
        offered = await _quality_menu(arq, found.url, settings)
        if len(offered) >= 2:
            await _offer_choice(arq, placeholder, job, offered, settings)
            return

    await _enqueue(arq, limits, job, settings, placeholder)


async def _quality_menu(arq: ArqRedis, url: str, settings: Settings) -> list[FormatOption]:
    """Ask the platform worker for formats (cached for a few minutes); [] when unknown."""
    cached = await arq.get(probe_key(url))
    if cached:
        with contextlib.suppress(ValueError):
            return [FormatOption.model_validate(o) for o in json.loads(cached)]
    try:
        handle = await arq.enqueue_job(
            PROBE_FUNCTION,
            url,
            _queue_name=QUEUE_NAMES[Platform.YOUTUBE],
            _expires=settings.extract_timeout,
        )
        if handle is None:  # same probe already queued: wait for it instead
            return []
        raw = await handle.result(timeout=settings.extract_timeout + 10, poll_delay=0.25)
        result = ProbeResult.model_validate(raw)
    except Exception as e:  # timeout, worker gone, bad payload: fall back to automatic quality
        log.warning("probe_unavailable", url=url, error=repr(e))
        return []
    offered = [
        f for f in result.formats if f.size_bytes is None or f.size_bytes <= settings.max_file_bytes
    ]
    await arq.set(
        probe_key(url),
        json.dumps([f.model_dump(mode="json") for f in offered]),
        ex=settings.probe_cache_ttl,
    )
    return offered


def quality_keyboard(job_id: str, offered: list[FormatOption]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f.label, callback_data=f"q:{job_id}:{f.quality.value}")]
        for f in offered
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _offer_choice(
    arq: ArqRedis, placeholder: Message, job: Job, offered: list[FormatOption], settings: Settings
) -> None:
    default = offered[0].quality  # best rung under the cap comes first
    pending = PendingChoice(job=job, default=default, offered=[f.quality for f in offered])
    await arq.set(
        pending_key(job.job_id), pending.model_dump_json(), ex=settings.quality_choice_timeout + 30
    )
    await placeholder.edit_text(
        f"Pick a quality (I'll go with {offered[0].label} in {settings.quality_choice_timeout}s):",
        reply_markup=quality_keyboard(job.job_id, offered),
    )
    asyncio.create_task(_choose_default_later(arq, placeholder, job.job_id, settings))


async def _choose_default_later(
    arq: ArqRedis, placeholder: Message, job_id: str, settings: Settings
) -> None:
    await asyncio.sleep(settings.quality_choice_timeout)
    pending = await _take_pending(arq, job_id)
    if pending is None:
        return  # the user already chose
    pending.job.quality = pending.default
    with contextlib.suppress(TelegramBadRequest):
        await placeholder.edit_text(WORKING)
    await _enqueue_pending(arq, pending.job, settings)


async def _take_pending(arq: ArqRedis, job_id: str) -> PendingChoice | None:
    raw = await arq.getdel(pending_key(job_id))
    return PendingChoice.model_validate_json(raw) if raw else None


@router.callback_query(F.data.startswith("q:"))
async def on_quality_chosen(
    query: CallbackQuery, settings: Settings, arq: ArqRedis, limits: UserLimits
) -> None:
    try:
        _, job_id, raw_quality = (query.data or "").split(":", 2)
        quality = Quality(raw_quality)
    except ValueError:
        await query.answer("Bad choice")
        return
    pending = await _take_pending(arq, job_id)
    if pending is None:
        await query.answer("That menu expired. Send the link again.", show_alert=True)
        return
    if query.from_user.id != pending.job.user_id:
        await arq.set(
            pending_key(job_id), pending.model_dump_json(), ex=settings.quality_choice_timeout
        )
        await query.answer("Only the person who sent the link can choose.")
        return
    pending.job.quality = quality if quality in pending.offered else pending.default
    await query.answer()
    if isinstance(query.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await query.message.edit_text(WORKING)
    await _enqueue_pending(arq, pending.job, settings)


async def _enqueue_pending(arq: ArqRedis, job: Job, settings: Settings) -> None:
    try:
        await arq.enqueue_job(
            JOB_FUNCTION,
            job.model_dump(mode="json"),
            _job_id=job.job_id,
            _queue_name=QUEUE_NAMES[job.platform],
            _expires=settings.job_ttl,
        )
        log.info("job_enqueued", job_id=job.job_id, platform=job.platform, quality=job.quality)
    except Exception:
        log.exception("enqueue_failed", job_id=job.job_id)


async def _enqueue(
    arq: ArqRedis, limits: UserLimits, job: Job, settings: Settings, placeholder: Message
) -> None:
    try:
        await arq.enqueue_job(
            JOB_FUNCTION,
            job.model_dump(mode="json"),
            _job_id=job.job_id,
            _queue_name=QUEUE_NAMES[job.platform],
            _expires=settings.job_ttl,
        )
    except Exception:
        log.exception("enqueue_failed", job_id=job.job_id)
        await limits.release(job.user_id)
        await placeholder.edit_text("❌ Couldn't queue that right now. Try again in a minute.")
        return
    log.info("job_enqueued", job_id=job.job_id, platform=job.platform, quality=job.quality)
