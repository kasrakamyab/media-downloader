"""The worker pattern every platform worker copies.

A platform worker provides one `Extractor` (download URL into a folder, return file paths)
and gets everything else from here: cache lookup, size cap, temp-folder lifecycle, Telegram
delivery, result bookkeeping, heartbeat, janitor and arq wiring.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from arq.connections import RedisSettings
from arq.cron import cron
from arq.worker import func
from redis.asyncio import Redis

from core.cache import FileCache
from core.contracts import (
    ERROR_MESSAGES,
    JOB_FUNCTION,
    QUEUE_NAMES,
    ErrorCode,
    Job,
    Platform,
    Result,
)
from core.health import beat, record_result
from core.limits import UserLimits
from core.logging import configure_logging, get_logger
from core.settings import Settings, get_settings
from core.telegram import Sender, TelegramSender, make_bot

log = get_logger("worker")

# Extractor: (job, dest_dir, settings) -> list of downloaded files. Must raise ExtractError.
Extractor = Callable[[Job, Path, Settings], Awaitable[list[Path]]]


class ExtractError(Exception):
    def __init__(self, code: ErrorCode, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def collect_files(dest: Path) -> list[Path]:
    """Every regular file under dest, sorted, ignoring extractor leftovers."""
    skip = {".part", ".ytdl", ".json", ".txt", ".description"}
    return sorted(p for p in dest.rglob("*") if p.is_file() and p.suffix.lower() not in skip)


def free_bytes(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def janitor_sweep(job_dir: Path, max_age_s: int = 600) -> int:
    """Delete job folders older than max_age_s. Returns how many were removed."""
    if not job_dir.exists():
        return 0
    cutoff = time.time() - max_age_s
    removed = 0
    for d in job_dir.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


async def run_job(
    job: Job,
    *,
    extractor: Extractor,
    sender: Sender,
    cache: FileCache,
    limits: UserLimits,
    redis: Redis,
    settings: Settings,
) -> Result:
    """Process one job end to end. Never raises; always returns a Result."""
    started = time.monotonic()
    log_ = log.bind(job_id=job.job_id, platform=job.platform, url=job.url)
    dest = Path(settings.job_dir) / job.job_id
    result: Result

    try:
        if await cache.is_blocked(job.url):
            raise ExtractError(ErrorCode.UNSUPPORTED_URL, "blocked")

        cached = await cache.get(job.url, job.quality)
        if cached:
            await sender.send_cached(job.chat_id, cached, None)
            await sender.finish(job.chat_id, job.message_id, None)
            result = Result.ok(job.job_id, cached, _ms(started), cached=True)
            log_.info("served_from_cache", took_ms=result.took_ms)
            return result

        if free_bytes(Path(settings.job_dir)) < settings.min_free_gb * 1024**3:
            raise ExtractError(ErrorCode.SOURCE_DOWN, "low disk")

        dest.mkdir(parents=True, exist_ok=True)
        try:
            files = await asyncio.wait_for(
                extractor(job, dest, settings),
                timeout=settings.extract_timeout + settings.download_timeout,
            )
        except TimeoutError as e:
            raise ExtractError(ErrorCode.SOURCE_DOWN, "timeout") from e

        files = [f for f in files if f.is_file() and f.stat().st_size > 0]
        if not files:
            raise ExtractError(ErrorCode.UNKNOWN, "no files produced")
        too_big = [f for f in files if f.stat().st_size > settings.max_file_bytes]
        if too_big:
            raise ExtractError(ErrorCode.TOO_LARGE, too_big[0].name)

        file_ids = await sender.send_files(job.chat_id, files, None)
        await cache.set(job.url, job.quality, file_ids)
        await sender.finish(job.chat_id, job.message_id, None)
        result = Result.ok(job.job_id, file_ids, _ms(started))
        log_.info("job_done", files=len(files), took_ms=result.took_ms)
        return result

    except ExtractError as e:
        result = Result.error(job.job_id, e.code, _ms(started))
        log_.warning("job_failed", code=e.code, detail=e.detail)
        await _safe_finish(sender, job, ERROR_MESSAGES[e.code])
        return result
    except Exception:
        result = Result.error(job.job_id, ErrorCode.UNKNOWN, _ms(started))
        log_.exception("job_crashed")
        await _safe_finish(sender, job, ERROR_MESSAGES[ErrorCode.UNKNOWN])
        return result
    finally:
        shutil.rmtree(dest, ignore_errors=True)
        try:
            await limits.release(job.user_id)
            await record_result(redis, job.platform, result)  # type: ignore[possibly-undefined]
        except Exception:
            log_.exception("bookkeeping_failed")


async def _safe_finish(sender: Sender, job: Job, text: str) -> None:
    try:
        await sender.finish(job.chat_id, job.message_id, f"❌ {text}")
    except Exception:
        log.exception("finish_failed", job_id=job.job_id)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def build_worker_settings(platform: Platform, extractor: Extractor) -> type:
    """Return an arq `WorkerSettings` class for one platform."""
    settings = get_settings()

    async def process_job(ctx: dict[str, Any], job_data: dict[str, Any]) -> dict[str, Any]:
        job = Job.model_validate(job_data)
        result = await run_job(
            job,
            extractor=extractor,
            sender=ctx["sender"],
            cache=ctx["cache"],
            limits=ctx["limits"],
            redis=ctx["redis"],
            settings=settings,
        )
        return result.model_dump(mode="json")

    async def heartbeat(ctx: dict[str, Any]) -> None:
        await beat(ctx["redis"], platform, settings.heartbeat_ttl)

    async def janitor(ctx: dict[str, Any]) -> None:
        removed = await asyncio.to_thread(janitor_sweep, Path(settings.job_dir))
        if removed:
            log.info("janitor_removed", folders=removed)

    async def on_startup(ctx: dict[str, Any]) -> None:
        configure_logging(settings.log_level, settings.log_json)
        await asyncio.to_thread(Path(settings.job_dir).mkdir, parents=True, exist_ok=True)
        bot = make_bot(settings.bot_token, settings.telegram_api_url)
        ctx["bot"] = bot
        ctx["sender"] = TelegramSender(bot)
        ctx["cache"] = FileCache(ctx["redis"], settings.cache_ttl_days)
        ctx["limits"] = UserLimits(ctx["redis"], settings.max_active_jobs, settings.hourly_limit)
        await heartbeat(ctx)
        log.info("worker_started", platform=platform, queue=QUEUE_NAMES[platform])

    async def on_shutdown(ctx: dict[str, Any]) -> None:
        bot = ctx.get("bot")
        if bot:
            await bot.session.close()
        log.info("worker_stopped", platform=platform)

    class WorkerSettings:
        # arq registers by qualname, so name the job function explicitly: the gateway
        # enqueues JOB_FUNCTION and every platform worker must answer to it.
        functions = [func(process_job, name=JOB_FUNCTION)]
        cron_jobs = [
            cron(heartbeat, name="heartbeat", second={0, 20, 40}, run_at_startup=True),
            cron(janitor, name="janitor", minute=set(range(0, 60, 2))),
        ]
        log_results = False  # heartbeat would log three lines a minute
        queue_name = QUEUE_NAMES[platform]
        redis_settings = RedisSettings.from_dsn(settings.redis_url)
        max_jobs = settings.worker_concurrency
        job_timeout = settings.extract_timeout + settings.download_timeout + 60
        max_tries = 1  # the gateway already told the user; never silently retry a download
        keep_result = 0
        health_check_interval = 30

    WorkerSettings.on_startup = staticmethod(on_startup)  # type: ignore[attr-defined]
    WorkerSettings.on_shutdown = staticmethod(on_shutdown)  # type: ignore[attr-defined]
    return WorkerSettings
