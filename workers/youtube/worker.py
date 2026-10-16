"""YouTube worker: yt-dlp with a quality ladder (720p -> 480p -> 360p -> audio).

Run with:  arq workers.youtube.worker.WorkerSettings
"""

from __future__ import annotations

from pathlib import Path

from core.contracts import Job, Platform
from core.extractors import ytdlp_download
from core.settings import Settings
from core.worker import build_worker_settings


async def extract(job: Job, dest: Path, settings: Settings) -> list[Path]:
    return await ytdlp_download(job.url, dest, job.quality, settings, settings.yt_cookies_file)


WorkerSettings = build_worker_settings(Platform.YOUTUBE, extract)
