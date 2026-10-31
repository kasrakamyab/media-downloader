"""Thin wrappers around yt-dlp and gallery-dl shared by the workers.

Both run off the event loop (thread / subprocess) and translate tool failures into
`ExtractError` codes from the taxonomy.
"""

from __future__ import annotations

import asyncio
import fcntl
import http.cookiejar
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path

from core.captions import MediaMeta, from_gallerydl, from_ytdlp
from core.contracts import ErrorCode, FormatOption, Quality
from core.logging import get_logger
from core.settings import Settings
from core.worker import Extracted, ExtractError, collect_files

log = get_logger("extractors")


def gallerydl_meta(dest: Path, files: list[Path], platform: str, url: str) -> MediaMeta | None:
    """Read the `--write-metadata` sidecar of the first file, if gallery-dl produced one."""
    for candidate in (Path(str(files[0]) + ".json"), *sorted(dest.glob("*.json"))):
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(errors="replace"))
            except ValueError:
                continue
            if isinstance(data, dict):
                return from_gallerydl(data, platform, url)
    return None


@contextmanager
def cookie_copy(
    cookies: str | None, workdir: Path, state_dir: str | None = None
) -> Iterator[str | None]:
    """Yield a private, writable copy of a cookies file (or None when there is none).

    yt-dlp and gallery-dl both write refreshed cookies back to the file they were given on
    exit. `./secrets` is mounted read-only on purpose, so hand them a per-call copy inside
    the job folder and let the original stay untouched.

    With `state_dir` (COOKIE_STATE_DIR), the copy comes from a writable state jar instead,
    and after a call that didn't raise, the cookies the site changed are merged back into
    it. YouTube rotates its session cookies (__Secure-*PSIDTS ...) every few hours; a jar
    that never takes the rotations is revoked, which is what killed fresh exports in ~2 h.
    """
    if not cookies:
        yield None
        return
    src = Path(cookies)
    if not src.is_file():
        raise ExtractError(ErrorCode.UNKNOWN, f"cookies file missing: {cookies}")
    state = _seed_state_jar(src, Path(state_dir)) if state_dir else None
    workdir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="cookies-", suffix=".txt", dir=workdir)
    os.close(fd)
    try:
        with _jar_lock(state) if state else nullcontext():
            shutil.copyfile(state or src, tmp)
        os.chmod(tmp, 0o600)
        before = _read_jar(Path(tmp)) if state else {}
        yield tmp
        if state:
            _merge_rotations(state, before, Path(tmp))
    finally:
        with_suppress_unlink(tmp)


# A cookie's identity in a jar, and the parts of it a rotation changes.
_CookieKey = tuple[str, str, str]  # domain, path, name


def _read_jar(path: Path) -> dict[_CookieKey, http.cookiejar.Cookie]:
    """Parse a Netscape jar (session cookies and #HttpOnly_ lines included)."""
    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError):
        return {}
    return {(c.domain, c.path, c.name): c for c in jar}


@contextmanager
def _jar_lock(state: Path) -> Iterator[None]:
    """Exclusive lock on the state jar, shared by every worker on the same volume."""
    with open(state.with_name(state.name + ".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _seed_state_jar(src: Path, state_dir: Path) -> Path:
    """The state jar for `src`, (re)seeded from it when it is new or `src` was replaced.

    The `.seed` sidecar remembers which export the jar came from (mtime + size), so a
    fresh export dropped into ./secrets takes over, while the rotations of the current
    one are kept across restarts.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    state = state_dir / src.name
    seed = state_dir / (src.name + ".seed")
    st = src.stat()
    stamp = f"{st.st_mtime_ns} {st.st_size}"
    with _jar_lock(state):
        current = seed.read_text().strip() if seed.is_file() else None
        if current != stamp or not state.is_file():
            _atomic_copy(src, state)
            seed.write_text(stamp)
            log.info("cookie_state_seeded", jar=str(state))
    return state


def _merge_rotations(
    state: Path, before: dict[_CookieKey, http.cookiejar.Cookie], after_path: Path
) -> None:
    """Write the cookies this call changed (new value or expiry, or new cookies) into the
    state jar. Only the changes: a job that started from an older jar must not undo a
    rotation another job merged meanwhile. Expired cookies and deletions are ignored."""
    after = _read_jar(after_path)
    now = time.time()
    changed = [
        c
        for key, c in after.items()
        if (old := before.get(key)) is None or (old.value, old.expires) != (c.value, c.expires)
        if c.expires is None or c.expires > now
    ]
    if not changed:
        return
    with _jar_lock(state):
        jar = http.cookiejar.MozillaCookieJar(str(state))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError):
            return  # never replace a jar we can't read with a partial one
        for c in changed:
            jar.set_cookie(c)
        tmp = state.with_name(state.name + ".tmp")
        jar.save(str(tmp), ignore_discard=True, ignore_expires=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, state)
    log.info("cookie_rotations_saved", jar=str(state), changed=len(changed))


def _atomic_copy(src: Path, dst: Path) -> None:
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copyfile(src, tmp)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dst)


def with_suppress_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


_LOGIN_RE = re.compile(
    r"login|log in|sign in|logged[- ]in|cookies|authentication|requires? (?:an? )?account",
    re.IGNORECASE,
)
# Checked before the login words: "Private video. Sign in if you've been granted access"
# is about the post, not about our session.
_PRIVATE_STRONG_RE = re.compile(r"private|members-only|age[- ]restricted", re.IGNORECASE)
_PRIVATE_RE = re.compile(
    r"not available|requested content is not available|unavailable|removed|404|not found|403",
    re.IGNORECASE,
)
_RATE_RE = re.compile(r"429|too many requests|rate[- ]limit", re.IGNORECASE)
_NET_RE = re.compile(
    r"timed out|timeout|connection|network|unable to download|proxy|HTTP Error 5\d\d|"
    r"temporar|resolve",
    re.IGNORECASE,
)


NO_FORMAT = "requested format is not available"
_JS_RE = re.compile(
    r"deno|node|javascript runtime|js runtime|jsi|challenge|signature|n function|nsig|"
    r"ejs|remote component",
    re.IGNORECASE,
)


class YtdlpLog:
    """Collects yt-dlp's warnings so a failure can be explained. When the JS runtime that
    solves YouTube's challenges dies, yt-dlp only warns ("Signature solving failed",
    "challenge", "deno …") and then every real format is missing; without the warning the
    user would just see "format not available"."""

    def __init__(self) -> None:
        self.last_js_warning = ""

    def debug(self, msg: str) -> None:  # yt-dlp sends its normal output here too
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        if _JS_RE.search(msg):
            self.last_js_warning = msg

    def error(self, msg: str) -> None:
        if _JS_RE.search(msg):
            self.last_js_warning = msg

    @property
    def js_runtime_failed(self) -> bool:
        return bool(self.last_js_warning)


def classify(message: str) -> ErrorCode:
    lowered = message.lower()
    if "unsupported url" in lowered:
        return ErrorCode.UNSUPPORTED_URL
    if NO_FORMAT in lowered:
        # Our selector matched nothing; says nothing about the post. Must not read as
        # PRIVATE through the generic "not available" pattern below.
        return ErrorCode.UNKNOWN
    if _RATE_RE.search(message):
        return ErrorCode.RATE_LIMITED
    if _NET_RE.search(message):  # before PRIVATE: a proxy 403 is the network, not the post
        return ErrorCode.SOURCE_DOWN
    if _PRIVATE_STRONG_RE.search(message):
        return ErrorCode.PRIVATE
    if _LOGIN_RE.search(message):  # "redirect to login page", "Sign in to confirm…"
        return ErrorCode.LOGIN_REQUIRED
    if _PRIVATE_RE.search(message):
        return ErrorCode.PRIVATE
    return ErrorCode.UNKNOWN


# ---- yt-dlp -------------------------------------------------------------------------

# Rungs tried in order when quality is BEST; a chosen quality starts at its rung.
LADDER: list[Quality] = [Quality.P720, Quality.P480, Quality.P360, Quality.AUDIO]
_HEIGHT = {Quality.P1080: 1080, Quality.P720: 720, Quality.P480: 480, Quality.P360: 360}


def ytdlp_format(quality: Quality) -> str:
    """Format selector: prefer mp4 video + m4a audio, but accept any video-only + any
    audio-only pair (Pinterest HLS has video-only mp4 streams and an mp4 audio stream, no
    m4a and no muxed format) before falling back to a muxed file. The size rung is applied
    through `ytdlp_sort`, not here, so vertical videos aren't judged by their height."""
    if quality == Quality.AUDIO:
        return "ba[ext=m4a]/ba/b"
    return "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b[ext=mp4]/b/bv*"


def ytdlp_sort(quality: Quality) -> list[str]:
    """`format_sort` for a rung: `res:<h>` prefers the best format whose *shorter* side is
    at most h (so a 720x1280 portrait counts as 720p, not 1280p); then plain https over
    HLS/DASH segments — an HLS stream reports only filesize_approx and yt-dlp can't enforce
    max_filesize on it, so a muxed m3u8 with a fat bitrate would win and blow the cap; then
    mp4/m4a, then bitrate. Formats above the rung (or HLS-only sources such as Pinterest)
    are still considered, just later."""
    if quality == Quality.AUDIO:
        return ["abr", "proto", "ext:m4a"]
    h = _HEIGHT.get(quality, 720)
    return [f"res:{h}", "proto", "ext:mp4:m4a", "tbr"]


def ladder_for(quality: Quality) -> list[Quality]:
    if quality == Quality.BEST:
        return LADDER
    if quality in LADDER:
        return LADDER[LADDER.index(quality) :]
    return [quality, *LADDER]


def ytdlp_common_opts(settings: Settings, cookies: str | None) -> dict:
    """Options shared by downloads and probes: network, cookies, PO-token provider."""
    opts: dict = {
        "noplaylist": True,
        "playlist_items": "1",
        "socket_timeout": settings.extract_timeout,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    # `cookies` is resolved to a writable copy by the callers (see cookie_copy); the
    # original path is never handed to yt-dlp.
    if settings.download_proxy:
        opts["proxy"] = settings.download_proxy
    if settings.pot_provider_url:
        opts["extractor_args"] = {
            "youtubepot-bgutilhttp": {"base_url": [settings.pot_provider_url.rstrip("/")]}
        }
    return opts


def _ytdlp_once(
    url: str, dest: Path, quality: Quality, settings: Settings, cookies: str | None
) -> tuple[list[Path], dict]:
    """Download one rung; returns the files and yt-dlp's info dict (for the caption)."""
    import yt_dlp  # imported lazily: heavy, and tests don't need it

    opts: dict = ytdlp_common_opts(settings, cookies) | {
        "format": ytdlp_format(quality),
        "format_sort": ytdlp_sort(quality),
        "outtmpl": str(dest / "%(id)s.%(ext)s"),
        "max_filesize": settings.max_file_bytes,
        "merge_output_format": "mp4" if quality != Quality.AUDIO else None,
        "retries": 2,
        "restrictfilenames": True,
    }
    if settings.rate_limit:
        opts["ratelimit"] = yt_dlp.utils.parse_bytes(settings.rate_limit)
    info: dict = {}
    warnings = YtdlpLog()
    opts["logger"] = warnings
    try:
        with cookie_copy(cookies, dest, settings.cookie_state_dir) as jar:
            if jar:
                opts["cookiefile"] = jar
            with yt_dlp.YoutubeDL(opts) as ydl:
                raw = ydl.extract_info(url, download=True)
                info = ydl.sanitize_info(raw) if raw else {}
    except yt_dlp.utils.DownloadError as e:
        detail = str(e)
        if NO_FORMAT in detail.lower() and warnings.js_runtime_failed:
            # The JS challenge solver (deno) died — on a server almost always the memory
            # limit — so only storyboard images were left to choose from.
            log.error("js_runtime_failed", url=url, warning=warnings.last_js_warning[:200])
            detail = (
                "YouTube JS challenge solver failed (deno; usually OOM-killed — raise "
                f"worker-youtube's mem_limit, see docs/deploy.md): {warnings.last_js_warning}"
            )
        raise ExtractError(classify(str(e)), detail) from e
    except OSError as e:
        # Disk full, permissions, a mount gone read-only: a configuration problem, not
        # something a retry on the next rung would change.
        raise ExtractError(ErrorCode.UNKNOWN, f"filesystem error: {e}") from e
    if info.get("_type") == "playlist":  # playlist_items=1: the one entry we fetched
        entries = info.get("entries") or []
        info = entries[0] if entries and isinstance(entries[0], dict) else {}
    return collect_files(dest), info or {}


async def ytdlp_download(
    url: str, dest: Path, quality: Quality, settings: Settings, cookies: str | None = None
) -> Extracted:
    """Walk the quality ladder until something under the size cap downloads."""
    last: ExtractError | None = None
    for rung in ladder_for(quality):
        try:
            files, info = await asyncio.to_thread(_ytdlp_once, url, dest, rung, settings, cookies)
        except ExtractError as e:
            if e.code in {
                ErrorCode.PRIVATE,
                ErrorCode.LOGIN_REQUIRED,
                ErrorCode.UNSUPPORTED_URL,
                ErrorCode.RATE_LIMITED,
            }:
                raise
            if NO_FORMAT in e.detail.lower():
                log.info("no_matching_format", url=url, rung=rung, selector=ytdlp_format(rung))
            last = e
            continue
        if files:
            size = sum(f.stat().st_size for f in files)
            if size > settings.max_file_bytes:
                # HLS downloads get past max_filesize (yt-dlp can't know their size up
                # front): treat an oversized result like yt-dlp's own skip and try the
                # next rung instead of handing the worker a file it will refuse.
                log.info("rung_over_cap", url=url, rung=rung, bytes=size)
                for f in files:
                    f.unlink(missing_ok=True)
                last = ExtractError(ErrorCode.TOO_LARGE, f"{rung}: {size} B over size cap")
                continue
            return Extracted(files, meta=from_ytdlp(info, size) if info else None)
        # yt-dlp silently skips files over max_filesize: try the next rung.
        last = ExtractError(ErrorCode.TOO_LARGE, f"{rung} over size cap")
    raise last or ExtractError(ErrorCode.UNKNOWN, "ladder exhausted")


def _fmt_size(f: dict, duration: float | None) -> int | None:
    size = f.get("filesize") or f.get("filesize_approx")
    if size:
        return int(size)
    tbr = f.get("tbr")
    if tbr and duration:
        return int(tbr * 1000 / 8 * duration)
    return None


def formats_from_info(info: dict) -> list[FormatOption]:
    """Turn yt-dlp's `formats` list into the quality menu: one entry per distinct rung."""
    formats = [f for f in info.get("formats") or [] if f.get("format_id")]
    duration = info.get("duration")
    audio_only = [
        f
        for f in formats
        if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")
    ]
    best_audio = max(audio_only, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)
    audio_size = _fmt_size(best_audio, duration) if best_audio else None

    options: list[FormatOption] = []
    seen_heights: set[int] = set()
    for quality, cap in (
        (Quality.P1080, 1080),
        (Quality.P720, 720),
        (Quality.P480, 480),
        (Quality.P360, 360),
    ):
        vids = [
            f
            for f in formats
            if f.get("vcodec") not in (None, "none") and 0 < (f.get("height") or 0) <= cap
        ]
        if not vids:
            continue
        top = max(vids, key=lambda f: ((f.get("height") or 0), f.get("tbr") or 0))
        height = int(top["height"])
        if height in seen_heights:
            continue  # e.g. a 720p-max video: the 1080p rung would just repeat 720p
        seen_heights.add(height)
        size = _fmt_size(top, duration)
        if size is not None and top.get("acodec") in (None, "none") and audio_size:
            size += audio_size  # video-only stream gets muxed with the audio stream
        options.append(FormatOption(quality=quality, size_bytes=size, height=height))
    if best_audio:
        options.append(FormatOption(quality=Quality.AUDIO, size_bytes=audio_size))
    return options


def _ytdlp_probe_sync(url: str, settings: Settings, cookies: str | None) -> tuple[list, str | None]:
    import yt_dlp

    opts = ytdlp_common_opts(settings, cookies) | {"skip_download": True}
    try:
        with cookie_copy(cookies, Path(settings.job_dir), settings.cookie_state_dir) as jar:
            if jar:
                opts["cookiefile"] = jar
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise ExtractError(classify(str(e)), str(e)) from e
    except OSError as e:
        raise ExtractError(ErrorCode.UNKNOWN, f"filesystem error: {e}") from e
    if info and info.get("_type") == "playlist":
        entries = info.get("entries") or []
        info = entries[0] if entries else {}
    return formats_from_info(info or {}), (info or {}).get("title")


async def ytdlp_probe(
    url: str, settings: Settings, cookies: str | None = None
) -> tuple[list[FormatOption], str | None]:
    """List available qualities without downloading. Raises ExtractError."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_ytdlp_probe_sync, url, settings, cookies),
            timeout=settings.extract_timeout + 5,
        )
    except TimeoutError as e:
        raise ExtractError(ErrorCode.SOURCE_DOWN, "probe timeout") from e


# ---- gallery-dl ---------------------------------------------------------------------

_GDL_EXIT = {
    4: ErrorCode.SOURCE_DOWN,  # HTTP error (refined by stderr)
    8: ErrorCode.SOURCE_DOWN,  # network error
    16: ErrorCode.LOGIN_REQUIRED,  # authentication
    64: ErrorCode.UNSUPPORTED_URL,  # no extractor
}


async def gallerydl_download(
    url: str,
    dest: Path,
    settings: Settings,
    cookies: str | None = None,
    max_items: int = 10,
    platform: str = "",
) -> Extracted:
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
        "--write-metadata",  # <file>.json next to each file: caption source
    ]
    if settings.rate_limit:
        cmd += ["--limit-rate", settings.rate_limit]
    if settings.download_proxy:
        cmd += ["--proxy", settings.download_proxy]

    # gallery-dl writes refreshed cookies back too (cookies-update), so it gets a copy.
    with cookie_copy(cookies, dest, settings.cookie_state_dir) as jar:
        if jar:
            cmd += ["--cookies", jar]
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
    if files:  # exit 0, or partial success (e.g. one item over the cap): still a success
        return Extracted(files, meta=gallerydl_meta(dest, files, platform, url))
    code = _GDL_EXIT.get(proc.returncode or 1, ErrorCode.UNKNOWN)
    if proc.returncode in (1, 4):
        code = classify(stderr) if stderr.strip() else code
    if code == ErrorCode.LOGIN_REQUIRED and cookies:
        # We did send a session and it was still refused: the cookies have expired.
        raise ExtractError(code, "cookies rejected: " + stderr.strip()[-400:])
    raise ExtractError(code, stderr.strip()[-500:])
