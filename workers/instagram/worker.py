"""Instagram worker: gallery-dl first (photos, carousels, reels), yt-dlp fallback for video.

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


async def extract(job: Job, dest: Path, settings: Settings) -> list[Path]:
    cookies = settings.ig_cookies_file
    try:
        return await gallerydl_download(job.url, dest, settings, cookies, max_items=10)
    except ExtractError as e:
        if e.code in {ErrorCode.PRIVATE, ErrorCode.RATE_LIMITED}:
            raise
        # LOGIN_REQUIRED falls through on purpose: yt-dlp reaches public reels through
        # different endpoints and sometimes succeeds where gallery-dl hits the login wall.
        log.info("gallerydl_failed_trying_ytdlp", job_id=job.job_id, code=e.code)
        try:
            return await ytdlp_download(job.url, dest, Quality.BEST, settings, cookies)
        except ExtractError as e2:
            # Keep the more specific code from the first attempt when the fallback is vague.
            raise e2 if e2.code != ErrorCode.UNKNOWN else e from e2


WorkerSettings = build_worker_settings(Platform.INSTAGRAM, extract)
