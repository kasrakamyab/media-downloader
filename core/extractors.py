"""Thin wrappers around yt-dlp and gallery-dl shared by the workers.

Both run off the event loop (thread / subprocess) and translate tool failures into
`ExtractError` codes from the taxonomy.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from core.contracts import ErrorCode, Quality
from core.settings import Settings
from core.worker import ExtractError, collect_files

_PRIVATE_RE = re.compile(
    r"private|login|log in|sign in|not available|requested content is not available|"
    r"members-only|age[- ]restricted|unavailable|removed|404|not found|rate-limit|403",
    re.IGNORECASE,
)
_RATE_RE = re.compile(r"429|too many requests|rate limit", re.IGNORECASE)
_NET_RE = re.compile(
    r"timed out|timeout|connection|network|unable to download|proxy|HTTP Error 5\d\d|"
    r"temporar|resolve",
    re.IGNORECASE,
)


def classify(message: str) -> ErrorCode:
    if "unsupported url" in message.lower():
        return ErrorCode.UNSUPPORTED_URL
    if _RATE_RE.search(message):
        return ErrorCode.RATE_LIMITED
    if _NET_RE.search(message):  # before PRIVATE: a proxy 403 is the network, not the post
        return ErrorCode.SOURCE_DOWN
    if _PRIVATE_RE.search(message):
        return ErrorCode.PRIVATE
    return ErrorCode.UNKNOWN


# ---- yt-dlp -------------------------------------------------------------------------

# Rungs tried in order when quality is BEST; a chosen quality starts at its rung.
LADDER: list[Quality] = [Quality.P720, Quality.P480, Quality.P360, Quality.AUDIO]
_HEIGHT = {Quality.P1080: 1080, Quality.P720: 720, Quality.P480: 480, Quality.P360: 360}


def ytdlp_format(quality: Quality) -> str:
    if quality == Quality.AUDIO:
        return "ba[ext=m4a]/ba/b"
    h = _HEIGHT.get(quality, 720)
    return f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}][ext=mp4]/b[height<={h}]/b"


def ladder_for(quality: Quality) -> list[Quality]:
    if quality == Quality.BEST:
        return LADDER
    if quality in LADDER:
        return LADDER[LADDER.index(quality) :]
    return [quality, *LADDER]


def _ytdlp_once(
    url: str, dest: Path, quality: Quality, settings: Settings, cookies: str | None
) -> list[Path]:
    import yt_dlp  # imported lazily: heavy, and tests don't need it

    opts: dict = {
        "format": ytdlp_format(quality),
        "outtmpl": str(dest / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "max_filesize": settings.max_file_bytes,
        "merge_output_format": "mp4" if quality != Quality.AUDIO else None,
        "socket_timeout": settings.extract_timeout,
        "retries": 2,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "restrictfilenames": True,
        "playlist_items": "1",
    }
    if settings.rate_limit:
        opts["ratelimit"] = yt_dlp.utils.parse_bytes(settings.rate_limit)
    if cookies:
        opts["cookiefile"] = cookies
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as e:
        raise ExtractError(classify(str(e)), str(e)) from e
    return collect_files(dest)


async def ytdlp_download(
    url: str, dest: Path, quality: Quality, settings: Settings, cookies: str | None = None
) -> list[Path]:
    """Walk the quality ladder until something under the size cap downloads."""
    last: ExtractError | None = None
    for rung in ladder_for(quality):
        try:
            files = await asyncio.to_thread(_ytdlp_once, url, dest, rung, settings, cookies)
        except ExtractError as e:
            if e.code in {ErrorCode.PRIVATE, ErrorCode.UNSUPPORTED_URL, ErrorCode.RATE_LIMITED}:
                raise
            last = e
            continue
        if files:
            return files
        # yt-dlp silently skips files over max_filesize: try the next rung.
        last = ExtractError(ErrorCode.TOO_LARGE, f"{rung} over size cap")
    raise last or ExtractError(ErrorCode.UNKNOWN, "ladder exhausted")


# ---- gallery-dl ---------------------------------------------------------------------

_GDL_EXIT = {
    4: ErrorCode.SOURCE_DOWN,  # HTTP error (refined by stderr)
    8: ErrorCode.SOURCE_DOWN,  # network error
    16: ErrorCode.PRIVATE,  # authentication
    64: ErrorCode.UNSUPPORTED_URL,  # no extractor
}


async def gallerydl_download(
    url: str,
    dest: Path,
    settings: Settings,
    cookies: str | None = None,
    max_items: int = 10,
) -> list[Path]:
    cmd = [
        "gallery-dl",
        "--quiet",
        "--directory",
        str(dest),
        "--range",
        f"1-{max_items}",
        "--filesize-max",
        str(settings.max_file_bytes),
        "--http-timeout",
        str(settings.extract_timeout),
        "--retries",
        "1",
        "--no-part",
    ]
    if settings.rate_limit:
        cmd += ["--limit-rate", settings.rate_limit]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=settings.download_timeout)
    except TimeoutError:
        proc.kill()
        raise ExtractError(ErrorCode.SOURCE_DOWN, "gallery-dl timeout") from None
    stderr = err.decode(errors="replace")
    files = collect_files(dest)
    if proc.returncode == 0 and files:
        return files
    if files:  # partial success (e.g. one item over the cap) is still a success
        return files
    code = _GDL_EXIT.get(proc.returncode or 1, ErrorCode.UNKNOWN)
    if proc.returncode in (1, 4):
        code = classify(stderr) if stderr.strip() else code
    raise ExtractError(code, stderr.strip()[-500:])
