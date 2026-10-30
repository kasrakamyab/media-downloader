"""Instagram worker: gallery-dl (photos, carousels, reels), yt-dlp (reels), and the public
embed page as the cookie-less fallback for photo posts and carousels.

Run with:  arq workers.instagram.worker.WorkerSettings
"""

from __future__ import annotations

from pathlib import Path

from core.contracts import ErrorCode, Job, Platform, Quality
from core.extractors import gallerydl_download, ytdlp_download
from core.instagram_embed import embed_download, og_download
from core.logging import get_logger
from core.settings import Settings
from core.worker import ExtractError, build_worker_settings

log = get_logger("worker.instagram")

_FINAL = {ErrorCode.PRIVATE, ErrorCode.RATE_LIMITED}


async def _gallerydl(job: Job, dest: Path, settings: Settings, cookies: str | None) -> list[Path]:
    return await gallerydl_download(job.url, dest, settings, cookies, max_items=10)


async def _ytdlp(job: Job, dest: Path, settings: Settings, cookies: str | None) -> list[Path]:
    return await ytdlp_download(job.url, dest, Quality.BEST, settings, cookies)


async def _ogpage(job: Job, dest: Path, settings: Settings, cookies: str | None) -> list[Path]:
    return await og_download(job.url, dest, settings)


async def _embed(job: Job, dest: Path, settings: Settings, cookies: str | None) -> list[Path]:
    return await embed_download(job.url, dest, settings)


async def extract(job: Job, dest: Path, settings: Settings) -> list[Path]:
    cookies = settings.ig_cookies_file
    # With a session, gallery-dl handles everything. Without one it hits the login wall on
    # almost all posts; yt-dlp still gets public reels, and the embed page gets public
    # photos and carousels, so the order follows what has a chance first.
    # Cookie-less fallbacks come after the real extractors: the post page's og:image (single
    # photos, first item of carousels; works from exits where the rest is walled) and then
    # the embed page (full carousels from residential IPs, a login shell elsewhere). Both
    # fail fast and never make the error the user sees worse.
    chain = (
        (_gallerydl, _ytdlp, _ogpage, _embed) if cookies else (_ytdlp, _gallerydl, _ogpage, _embed)
    )
    first_error: ExtractError | None = None
    for step in chain:
        try:
            return await step(job, dest, settings, cookies)
        except ExtractError as e:
            if e.code in _FINAL:
                raise
            log.info("extractor_failed", job_id=job.job_id, code=e.code, tried=step.__name__)
            # Keep the most specific code: LOGIN_REQUIRED beats UNKNOWN from a later step.
            if first_error is None or (
                first_error.code == ErrorCode.UNKNOWN and e.code != ErrorCode.UNKNOWN
            ):
                first_error = e
    assert first_error is not None
    raise first_error


WorkerSettings = build_worker_settings(Platform.INSTAGRAM, extract)
