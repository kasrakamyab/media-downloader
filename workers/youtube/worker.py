"""YouTube worker: yt-dlp with a quality ladder (720p -> 480p -> 360p -> audio) and a probe
that lists real formats for the gateway's quality keyboard.

Run with:  arq workers.youtube.worker.WorkerSettings
"""

from __future__ import annotations

from pathlib import Path

from core.contracts import FormatOption, Job, Platform
from core.extractors import ytdlp_download, ytdlp_probe
from core.settings import Settings
from core.worker import build_worker_settings


async def extract(job: Job, dest: Path, settings: Settings) -> list[Path]:
    return await ytdlp_download(job.url, dest, job.quality, settings, settings.yt_cookies_file)


async def probe(url: str, settings: Settings) -> tuple[list[FormatOption], str | None]:
    return await ytdlp_probe(url, settings, settings.yt_cookies_file)


WorkerSettings = build_worker_settings(Platform.YOUTUBE, extract, prober=probe)
