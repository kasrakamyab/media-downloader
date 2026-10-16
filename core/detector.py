"""Platform detection and URL normalisation.

Normalisation makes the cache key stable: short links are expanded where the mapping is
static (youtu.be, instagr.am), tracking parameters are dropped, trailing slashes trimmed.
`pin.it` links can't be expanded without a network call, so they are kept and the
Pinterest worker resolves them before extraction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core.contracts import Platform

# Parameters that never change what the link points at.
_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "igshid",
    "igsh",
    "si",
    "feature",
    "ref",
    "ref_src",
    "pp",
    "ab_channel",
    "t",
    "rd",
    "invite_code",
    "sent",
}

_YT_ID = r"[A-Za-z0-9_-]{11}"

_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
_YOUTUBE_PATH = re.compile(rf"^/(watch|shorts/{_YT_ID}|embed/{_YT_ID}|v/{_YT_ID}|live/{_YT_ID})")
_INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
_INSTAGRAM_PATH = re.compile(r"^/(?:[A-Za-z0-9_.]+/)?(p|reel|reels|tv)/[A-Za-z0-9_-]+/?$")
_PINTEREST_HOST = re.compile(
    r"^(?:[a-z]{2,3}\.|www\.)?pinterest\.(?:com|[a-z]{2,3}|co\.[a-z]{2}|com\.[a-z]{2})$"
)
_PINTEREST_PATH = re.compile(r"^/pin/\d+/?$|^/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?$")
_PINTEREST_NOT_BOARD = {"pin", "search", "ideas", "today", "settings", "login", "videos"}

_URL_IN_TEXT = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Detected:
    platform: Platform
    url: str  # normalised


class UnsupportedURL(ValueError):
    pass


def find_urls(text: str) -> list[str]:
    """Return every http(s) URL in a message, in order."""
    return [m.group(0).rstrip(".,;:!?)") for m in _URL_IN_TEXT.finditer(text or "")]


def _clean_query(query: str, keep: set[str]) -> str:
    pairs = [(k, v) for k, v in parse_qsl(query, keep_blank_values=False) if k in keep]
    return urlencode(sorted(pairs))


def detect(raw: str) -> Detected:
    """Detect the platform of a URL and return its normalised form.

    Raises UnsupportedURL for anything we don't handle.
    """
    raw = (raw or "").strip()
    if not raw:
        raise UnsupportedURL("empty")
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = parts.hostname or ""
    host = host.lower()
    path = re.sub(r"/{2,}", "/", parts.path or "/")

    # --- YouTube ---------------------------------------------------------------------
    if host == "youtu.be":
        m = re.match(rf"^/({_YT_ID})", path)
        if m:
            return Detected(Platform.YOUTUBE, f"https://www.youtube.com/watch?v={m.group(1)}")
        raise UnsupportedURL(raw)
    if host in _YOUTUBE_HOSTS:
        if path == "/watch":
            q = dict(parse_qsl(parts.query))
            vid = q.get("v", "")
            if re.fullmatch(_YT_ID, vid):
                return Detected(Platform.YOUTUBE, f"https://www.youtube.com/watch?v={vid}")
            raise UnsupportedURL(raw)
        m = _YOUTUBE_PATH.match(path)
        if m:
            vid = path.split("/")[2]
            return Detected(Platform.YOUTUBE, f"https://www.youtube.com/watch?v={vid}")
        raise UnsupportedURL(raw)

    # --- Instagram -------------------------------------------------------------------
    if host == "instagr.am":
        host = "www.instagram.com"
    if host in _INSTAGRAM_HOSTS:
        m = _INSTAGRAM_PATH.match(path)
        if not m:
            raise UnsupportedURL(raw)
        kind = "reel" if m.group(1) == "reels" else m.group(1)
        shortcode = path.rstrip("/").split("/")[-1]
        return Detected(Platform.INSTAGRAM, f"https://www.instagram.com/{kind}/{shortcode}/")

    # --- Pinterest -------------------------------------------------------------------
    if host == "pin.it":
        code = path.strip("/")
        if re.fullmatch(r"[A-Za-z0-9]+", code):
            return Detected(Platform.PINTEREST, f"https://pin.it/{code}")
        raise UnsupportedURL(raw)
    if _PINTEREST_HOST.match(host):
        if not _PINTEREST_PATH.match(path):
            raise UnsupportedURL(raw)
        first = path.strip("/").split("/")[0]
        if first == "pin":
            if not re.match(r"^/pin/\d+/?$", path):
                raise UnsupportedURL(raw)
        elif first in _PINTEREST_NOT_BOARD:
            raise UnsupportedURL(raw)
        norm = "https://www.pinterest.com" + path.rstrip("/") + "/"
        return Detected(Platform.PINTEREST, norm)

    raise UnsupportedURL(raw)


def strip_tracking(url: str) -> str:
    """Generic tracking-param stripper for URLs that don't go through a platform rule."""
    parts = urlsplit(url)
    keep = {k for k, _ in parse_qsl(parts.query)} - _TRACKING_PARAMS
    return urlunsplit((parts.scheme, parts.netloc, parts.path, _clean_query(parts.query, keep), ""))
