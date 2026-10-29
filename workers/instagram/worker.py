"""Instagram worker: gallery-dl (photos, carousels, reels) and yt-dlp (reels) — the order
depends on whether we have a session.

Run with:  arq workers.instagram.worker.WorkerSettings
"""

from __future__ import annotations

from pathlib import Path

from core.contracts import ErrorCode, Job, Platform, Quality
from core.extractors import gallerydl_download, ytdlp_download
from core.logging import get_logger
from core.settings import Settings
from core.worker import ExtractError, build_worker_settings

log = get_logger("worker.instagram")

_FINAL = {ErrorCode.PRIVATE, ErrorCode.RATE_LIMITED}


async def _gallerydl(job: Job, dest: Path, settings: Settings, cookies: str | None) -> list[Path]:
    return await gallerydl_download(job.url, dest, settings, cookies, max_items=10)


async def _ytdlp(job: Job, dest: Path, settings: Settings, cookies: str | None) -> list[Path]:
    return await ytdlp_download(job.url, dest, Quality.BEST, settings, cookies)


async def extract(job: Job, dest: Path, settings: Settings) -> list[Path]:
    cookies = settings.ig_cookies_file
    # With a session, gallery-dl handles everything (carousels, photos, reels). Without
    # one it hits the login wall on almost all posts, while yt-dlp still reaches public
    # reels through other endpoints, so try the one that has a chance first.
    first, second = (_gallerydl, _ytdlp) if cookies else (_ytdlp, _gallerydl)
    try:
        return await first(job, dest, settings, cookies)
    except ExtractError as e:
        if e.code in _FINAL:
            raise
        log.info("first_extractor_failed", job_id=job.job_id, code=e.code, tried=first.__name__)
        try:
            return await second(job, dest, settings, cookies)
        except ExtractError as e2:
            # Keep the more specific code from the first attempt when the fallback is vague.
            raise e2 if e2.code != ErrorCode.UNKNOWN else e from e2


WorkerSettings = build_worker_settings(Platform.INSTAGRAM, extract)
