"""Telegram handlers. The gateway never downloads: detect -> limits -> placeholder -> enqueue."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from arq.connections import ArqRedis

from core.cache import FileCache
from core.contracts import JOB_FUNCTION, QUEUE_NAMES, Job, Platform, Quality
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

HELP = (
    "Send me a <b>YouTube</b>, <b>Instagram</b> or <b>Pinterest</b> link and I'll reply with "
    "the video or pictures.\n\n"
    "• YouTube: videos and Shorts, best quality under the size limit (720p → 480p → 360p)\n"
    "• Instagram: public posts, reels and carousels (up to 10 items)\n"
    "• Pinterest: pins and short boards (up to 10 items)\n\n"
    "Add <code>audio</code> after a YouTube link to get just the sound.\n\n"
    "Limits: {active} downloads at a time, {hourly} per hour.\n"
    "For personal use only. Private content is refused."
)


@router.message(CommandStart())
async def on_start(message: Message, settings: Settings) -> None:
    await message.answer(
        "👋 Hi! " + HELP.format(active=settings.max_active_jobs, hourly=settings.hourly_limit)
    )


@router.message(Command("help"))
async def on_help(message: Message, settings: Settings) -> None:
    await message.answer(HELP.format(active=settings.max_active_jobs, hourly=settings.hourly_limit))


@router.message(Command("stats"))
async def on_stats(message: Message, settings: Settings, arq: ArqRedis) -> None:
    if not message.from_user or message.from_user.id not in settings.admin_id_set:
        return
    lines = []
    for platform in Platform:
        s = await stats(arq, platform)
        alive = "🟢" if await is_alive(arq, platform) else "🔴"
        depth = await arq.llen(QUEUE_NAMES[platform])
        ok, err = s.get("ok", 0), s.get("error", 0)
        hits = s.get("cache_hit", 0)
        rate = f"{hits / ok:.0%}" if ok else "–"
        lines.append(
            f"{alive} <b>{PLATFORM_LABEL[platform]}</b> queue={depth} ok={ok} "
            f"err={err} cache={rate}"
        )
    await message.answer("\n".join(lines))


@router.message(F.text)
async def on_text(
    message: Message,
    settings: Settings,
    arq: ArqRedis,
    limits: UserLimits,
    cache: FileCache,
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

    if not await is_alive(arq, found.platform):
        others = [PLATFORM_LABEL[p] for p in Platform if p != found.platform]
        await message.answer(
            f"{PLATFORM_LABEL[found.platform]} is temporarily unavailable. "
            f"{' and '.join(others)} still work."
        )
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

    placeholder = await message.reply("⏳ Working on it…")
    job = Job(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        message_id=placeholder.message_id,
        url=found.url,
        platform=found.platform,
        quality=quality,
    )
    try:
        await arq.enqueue_job(
            JOB_FUNCTION,
            job.model_dump(mode="json"),
            _job_id=job.job_id,
            _queue_name=QUEUE_NAMES[found.platform],
            _expires=settings.job_ttl,
        )
    except Exception:
        log.exception("enqueue_failed", job_id=job.job_id)
        await limits.release(message.from_user.id)
        await placeholder.edit_text("❌ Couldn't queue that right now. Try again in a minute.")
        return
    log.info("job_enqueued", job_id=job.job_id, platform=found.platform, url=found.url)
