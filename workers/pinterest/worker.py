"""Pinterest worker: resolves pin.it short links, gallery-dl for pins/boards (max 10),
yt-dlp fallback for video pins.

Run with:  arq workers.pinterest.worker.WorkerSettings
"""

from __future__ import annotations

from pathlib import Path

import aiohttp

from core.contracts import ErrorCode, Job, Platform, Quality
from core.detector import UnsupportedURL, detect
from core.extractors import gallerydl_download, ytdlp_download
from core.logging import get_logger
from core.settings import Settings
from core.worker import ExtractError, build_worker_settings

log = get_logger("worker.pinterest")

BOARD_MAX_ITEMS = 10


async def resolve_short_link(url: str, timeout_s: int) -> str:
    """Follow pin.it redirects to the canonical pinterest.com URL."""
    if "pin.it/" not in url:
        return url
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as session,
            session.head(url, allow_redirects=True) as resp,
        ):
            final = str(resp.url)
    except (aiohttp.ClientError, TimeoutError) as e:
        raise ExtractError(ErrorCode.SOURCE_DOWN, f"pin.it resolve failed: {e}") from e
    try:
        return detect(final).url
    except UnsupportedURL as e:
        raise ExtractError(ErrorCode.UNSUPPORTED_URL, final) from e


async def extract(job: Job, dest: Path, settings: Settings) -> list[Path]:
    url = await resolve_short_link(job.url, settings.extract_timeout)
    cookies = settings.pin_cookies_file
    try:
        return await gallerydl_download(url, dest, settings, cookies, max_items=BOARD_MAX_ITEMS)
    except ExtractError as e:
        if e.code in {ErrorCode.PRIVATE, ErrorCode.RATE_LIMITED}:
            raise
        log.info("gallerydl_failed_trying_ytdlp", job_id=job.job_id, code=e.code)
        try:
            return await ytdlp_download(url, dest, Quality.BEST, settings, cookies)
        except ExtractError as e2:
            raise e2 if e2.code != ErrorCode.UNKNOWN else e from e2


WorkerSettings = build_worker_settings(Platform.PINTEREST, extract)
