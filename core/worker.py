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

from core.breaker import Breaker
from core.cache import FileCache
from core.contracts import (
    ERROR_MESSAGES,
    JOB_FUNCTION,
    PROBE_FUNCTION,
    QUEUE_NAMES,
    TRANSIENT_ERRORS,
    ErrorCode,
    FormatOption,
    Job,
    Platform,
    ProbeResult,
    Result,
)
from core.health import beat, is_alive, record_result
from core.healthserver import start_health_server
from core.limits import UserLimits
from core.logging import configure_logging, get_logger
from core.settings import Settings, get_settings
from core.telegram import (
    REACT_DONE,
    REACT_FAILED,
    DeliveryError,
    ForbiddenError,
    Sender,
    TelegramSender,
    make_bot,
)

log = get_logger("worker")

# Extractor: (job, dest_dir, settings) -> list of downloaded files. Must raise ExtractError.
Extractor = Callable[[Job, Path, Settings], Awaitable[list[Path]]]
# Prober: (url, settings) -> (format options, title). Only for platforms with a quality menu.
Prober = Callable[[str, Settings], Awaitable[tuple[list[FormatOption], str | None]]]


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
    breaker: Breaker | None = None,
) -> Result:
    """Process one job end to end. Never raises; always returns a Result."""
    started = time.monotonic()
    log_ = log.bind(job_id=job.job_id, platform=job.platform, url=job.url)
    dest = Path(settings.job_dir) / job.job_id
    result: Result
    breaker = breaker or Breaker(
        redis, settings.breaker_threshold, settings.breaker_window, settings.breaker_open_for
    )

    try:
        if await cache.is_blocked(job.url):
            raise ExtractError(ErrorCode.UNSUPPORTED_URL, "blocked")

        cached = await cache.get(job.url, job.quality)
        if cached:
            try:
                await sender.send_cached(
                    job.chat_id,
                    cached,
                    None,
                    reply_to=job.reply_to_message_id,
                    thread_id=job.thread_id,
                )
            except ForbiddenError as e:
                raise ExtractError(ErrorCode.FORBIDDEN, str(e)) from e
            await _finish_ok(sender, job)
            result = Result.ok(job.job_id, cached, _ms(started), cached=True)
            log_.info("served_from_cache", took_ms=result.took_ms)
            return result

        if free_bytes(Path(settings.job_dir)) < settings.min_free_gb * 1024**3:
            raise ExtractError(ErrorCode.SOURCE_DOWN, "low disk")

        files = await _extract_with_retries(job, dest, extractor, settings, log_)

        files = [f for f in files if f.is_file() and f.stat().st_size > 0]
        if not files:
            raise ExtractError(ErrorCode.UNKNOWN, "no files produced")
        too_big = [f for f in files if f.stat().st_size > settings.max_file_bytes]
        if too_big:
            raise ExtractError(ErrorCode.TOO_LARGE, too_big[0].name)

        try:
            file_ids = await sender.send_files(
                job.chat_id,
                files,
                None,
                reply_to=job.reply_to_message_id,
                thread_id=job.thread_id,
            )
        except ForbiddenError as e:
            raise ExtractError(ErrorCode.FORBIDDEN, str(e)) from e
        except DeliveryError as e:
            raise ExtractError(ErrorCode.UPLOAD_FAILED, str(e)) from e
        await cache.set(job.url, job.quality, file_ids)
        await _finish_ok(sender, job)
        result = Result.ok(job.job_id, file_ids, _ms(started))
        log_.info("job_done", files=len(files), took_ms=result.took_ms)
        return result

    except ExtractError as e:
        result = Result.error(job.job_id, e.code, _ms(started))
        log_.warning("job_failed", code=e.code, detail=e.detail)
        if e.code == ErrorCode.LOGIN_REQUIRED:
            await flag_login_required(redis, job.platform, e.detail)
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
            await limits.release(job.user_id, job.chat_id if job.is_group else None)
            await record_result(  # type: ignore[possibly-undefined]
                redis, job.platform, result, "group" if job.is_group else "private"
            )
            if result.status == "ok":
                await breaker.record_success(job.platform)
            elif result.error_code in TRANSIENT_ERRORS:
                if await breaker.record_failure(job.platform):
                    log_.error("breaker_opened", platform=job.platform)
        except Exception:
            log_.exception("bookkeeping_failed")


def login_flag_key(platform: Platform) -> str:
    return f"needs_login:{platform}"


async def flag_login_required(redis: Redis, platform: Platform, detail: str) -> None:
    """Tell the gateway's alert loop that this platform is asking for a session (no or
    expired cookies). Expires on its own so the alert repeats only if the problem persists."""
    await redis.set(login_flag_key(platform), detail[:200] or "login wall", ex=3600)


def _fresh_dir(dest: Path) -> None:
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)


async def _extract_with_retries(
    job: Job, dest: Path, extractor: Extractor, settings: Settings, log_: Any
) -> list[Path]:
    """Run the extractor; retry transient failures with linear backoff, nothing else."""
    attempts = 1 + max(settings.transient_retries, 0)
    per_attempt = settings.extract_timeout + settings.download_timeout
    for attempt in range(1, attempts + 1):
        await asyncio.to_thread(_fresh_dir, dest)
        try:
            return await asyncio.wait_for(extractor(job, dest, settings), timeout=per_attempt)
        except TimeoutError as e:
            err = ExtractError(ErrorCode.SOURCE_DOWN, "timeout")
            err.__cause__ = e
        except ExtractError as e:
            err = e
        if err.code not in TRANSIENT_ERRORS or attempt == attempts:
            raise err
        delay = settings.retry_backoff * attempt
        log_.info("retrying", attempt=attempt, code=err.code, delay_s=delay)
        await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def run_probe(url: str, *, prober: Prober, settings: Settings) -> ProbeResult:
    """List the quality menu for a URL. Never raises."""
    started = time.monotonic()
    try:
        formats, title = await prober(url, settings)
    except ExtractError as e:
        log.warning("probe_failed", url=url, code=e.code, detail=e.detail[:200])
        return ProbeResult(url=url, error_code=e.code, took_ms=_ms(started))
    except Exception:
        log.exception("probe_crashed", url=url)
        return ProbeResult(url=url, error_code=ErrorCode.UNKNOWN, took_ms=_ms(started))
    return ProbeResult(url=url, formats=formats, title=title, took_ms=_ms(started))


async def _finish_ok(sender: Sender, job: Job) -> None:
    if job.react_message_id:  # progress shown as a reaction on the user's message
        await sender.react(job.chat_id, job.react_message_id, REACT_DONE)
    if job.message_id:  # 0 = no placeholder (reactions, or auto mode in groups)
        await sender.finish(job.chat_id, job.message_id, None)


async def _safe_finish(sender: Sender, job: Job, text: str) -> None:
    """Tell the user why it failed: 👎 on their message when reactions are in use, plus the
    one-line reason — unless the job is quiet (auto mode), where the reason is only logged
    so the bot never adds noise to a group it wasn't asked in."""
    try:
        if job.react_message_id:
            await sender.react(job.chat_id, job.react_message_id, REACT_FAILED)
        if job.quiet:
            return
        if job.message_id:
            await sender.finish(job.chat_id, job.message_id, f"❌ {text}")
        elif job.react_message_id:
            await sender.say(
                job.chat_id,
                f"❌ {text}",
                reply_to=job.react_message_id,
                thread_id=job.thread_id,
            )
    except Exception:
        log.exception("finish_failed", job_id=job.job_id)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def build_worker_settings(
    platform: Platform, extractor: Extractor, prober: Prober | None = None
) -> type:
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
            breaker=ctx["breaker"],
        )
        return result.model_dump(mode="json")

    async def probe(ctx: dict[str, Any], url: str) -> dict[str, Any]:
        assert prober is not None
        result = await run_probe(url, prober=prober, settings=settings)
        return result.model_dump(mode="json")

    async def heartbeat_loop(redis: Redis) -> None:
        """Independent of arq's job slots: a worker that is merely busy must never read as
        down. Beats every third of the TTL so one missed beat is still inside it."""
        interval = max(settings.heartbeat_ttl // 3, 1)
        while True:
            try:
                await beat(redis, platform, settings.heartbeat_ttl)
            except Exception:
                log.exception("heartbeat_failed", platform=platform)
            await asyncio.sleep(interval)

    async def janitor(ctx: dict[str, Any]) -> None:
        removed = await asyncio.to_thread(janitor_sweep, Path(settings.job_dir))
        if removed:
            log.info("janitor_removed", folders=removed)

    async def on_startup(ctx: dict[str, Any]) -> None:
        configure_logging(settings.log_level, settings.log_json)
        await asyncio.to_thread(Path(settings.job_dir).mkdir, parents=True, exist_ok=True)
        bot = make_bot(settings.bot_token, settings.telegram_api_url, settings.telegram_proxy)
        ctx["bot"] = bot
        ctx["sender"] = TelegramSender(bot, settings.upload_timeout, settings.upload_seconds_per_mb)
        ctx["cache"] = FileCache(ctx["redis"], settings.cache_ttl_days)
        ctx["limits"] = UserLimits(
            ctx["redis"],
            settings.max_active_jobs,
            settings.hourly_limit,
            chat_max_active=settings.max_active_per_chat,
            chat_hourly=settings.group_hourly_limit,
        )
        ctx["breaker"] = Breaker(
            ctx["redis"],
            settings.breaker_threshold,
            settings.breaker_window,
            settings.breaker_open_for,
        )
        await beat(ctx["redis"], platform, settings.heartbeat_ttl)
        ctx["heartbeat"] = asyncio.create_task(heartbeat_loop(ctx["redis"]))
        cookies = {
            Platform.YOUTUBE: settings.yt_cookies_file,
            Platform.INSTAGRAM: settings.ig_cookies_file,
            Platform.PINTEREST: settings.pin_cookies_file,
        }[platform]
        if cookies and not await asyncio.to_thread(Path(cookies).is_file):
            log.error("cookies_file_missing", platform=platform, path=cookies)
        elif not cookies and platform == Platform.INSTAGRAM:
            log.warning("no_cookies", platform=platform, hint="public-only; expect login walls")
        if settings.health_port:

            async def health_probe() -> dict[str, Any]:
                return {
                    "ok": await is_alive(ctx["redis"], platform),
                    "service": f"worker-{platform}",
                    "breaker_open": await ctx["breaker"].is_open(platform),
                }

            ctx["health"] = await start_health_server(settings.health_port, health_probe)
        log.info("worker_started", platform=platform, queue=QUEUE_NAMES[platform])

    async def on_shutdown(ctx: dict[str, Any]) -> None:
        if task := ctx.get("heartbeat"):
            task.cancel()
        if runner := ctx.get("health"):
            await runner.cleanup()
        bot = ctx.get("bot")
        if bot:
            await bot.session.close()
        log.info("worker_stopped", platform=platform)

    attempts = 1 + max(settings.transient_retries, 0)
    per_attempt = settings.extract_timeout + settings.download_timeout + settings.retry_backoff

    class WorkerSettings:
        # arq registers by qualname, so name the job function explicitly: the gateway
        # enqueues JOB_FUNCTION and every platform worker must answer to it.
        functions = [func(process_job, name=JOB_FUNCTION, timeout=attempts * per_attempt + 60)]
        if prober is not None:
            # Probes are short and the gateway awaits their result, so keep it briefly.
            functions.append(
                func(
                    probe,
                    name=PROBE_FUNCTION,
                    timeout=settings.extract_timeout + 15,
                    keep_result=120,
                )
            )
        cron_jobs = [cron(janitor, name="janitor", minute=set(range(0, 60, 2)))]
        log_results = False
        queue_name = QUEUE_NAMES[platform]
        redis_settings = RedisSettings.from_dsn(settings.redis_url)
        max_jobs = settings.worker_concurrency
        max_tries = 1  # the gateway already told the user; never silently retry a download
        keep_result = 0
        health_check_interval = 30

    WorkerSettings.on_startup = staticmethod(on_startup)  # type: ignore[attr-defined]
    WorkerSettings.on_shutdown = staticmethod(on_shutdown)  # type: ignore[attr-defined]
    return WorkerSettings
