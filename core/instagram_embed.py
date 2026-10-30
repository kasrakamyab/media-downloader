"""Cookie-less fallbacks for public Instagram posts.

1. The post page itself (`instagram.com/p/<id>/`): a plain logged-out GET returns the
   server-rendered page whose `og:image` is the full-size photo (and `og:video` for reels)
   from exits where everything else is walled — verified live from a VPN exit. Single photos
   are covered; for carousels it is the first item only.
2. The embed page (`/p/<id>/embed/captioned/`): carries the full `shortcode_media` JSON
   (carousel children, video_url) from residential IPs, but from data-center / VPN exits it
   is a generic ~500 KB login shell with no media for every post (verified live). Kept as a
   last try.

Both are IP-dependent; on a server the reliable path is IG_COOKIES_FILE. Both fail fast with
LOGIN_REQUIRED when the page has no media, so the error the user sees never gets worse.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from core.contracts import ErrorCode
from core.logging import get_logger
from core.settings import Settings
from core.worker import ExtractError

log = get_logger("instagram.embed")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
MAX_ITEMS = 10
_SHORTCODE_RE = re.compile(r"instagram\.com/(?:[^/]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")
_ADDITIONAL_RE = re.compile(r"window\.__additionalDataLoaded\s*\(\s*[^,]+,\s*")
_CONTEXT_RE = re.compile(r'"contextJSON"\s*:\s*"')
_IMG_RE = re.compile(
    r'<img[^>]+class="[^"]*EmbeddedMediaImage[^"]*"[^>]+src="([^"]+)"|'
    r'<img[^>]+src="([^"]+)"[^>]+class="[^"]*EmbeddedMediaImage[^"]*"',
    re.IGNORECASE,
)
_VIDEO_RE = re.compile(r'<video[^>]+src="([^"]+)"', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class EmbedItem:
    url: str
    kind: str  # "image" | "video"


def embed_url(post_url: str) -> str | None:
    m = _SHORTCODE_RE.search(post_url)
    return f"https://www.instagram.com/p/{m.group(1)}/embed/captioned/" if m else None


# ---- parsing --------------------------------------------------------------------------


def _json_object_at(text: str, start: int) -> dict | None:
    """Parse the JSON object that begins at text[start] == '{', using the decoder's own
    boundary detection so trailing JS doesn't matter."""
    try:
        obj, _ = json.JSONDecoder().raw_decode(text, start)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _json_string_at(text: str, start: int) -> str | None:
    """Decode the JSON string literal whose opening quote is at text[start]."""
    try:
        value, _ = json.JSONDecoder().raw_decode(text, start)
    except ValueError:
        return None
    return value if isinstance(value, str) else None


def _find_shortcode_media(blob: object) -> dict | None:
    """Walk any JSON shape for a `shortcode_media` dict (graphql / gql_data / top level)."""
    if isinstance(blob, dict):
        if isinstance(blob.get("shortcode_media"), dict):
            return blob["shortcode_media"]
        for v in blob.values():
            found = _find_shortcode_media(v)
            if found:
                return found
    elif isinstance(blob, list):
        for v in blob:
            found = _find_shortcode_media(v)
            if found:
                return found
    return None


def _media_json(page: str) -> dict | None:
    # 1. window.__additionalDataLoaded('extra', {...});
    for m in _ADDITIONAL_RE.finditer(page):
        obj = _json_object_at(page, m.end())
        if obj and (sm := _find_shortcode_media(obj)):
            return sm
    # 2. "contextJSON":"{ ...escaped json... }"  (newer embed pages)
    for m in _CONTEXT_RE.finditer(page):
        inner = _json_string_at(page, m.end() - 1)
        if not inner:
            continue
        try:
            obj = json.loads(inner)
        except ValueError:
            continue
        if sm := _find_shortcode_media(obj):
            return sm
    # 3. a bare "shortcode_media":{...} anywhere
    idx = page.find('"shortcode_media":')
    while idx != -1:
        brace = page.find("{", idx)
        if brace != -1 and (obj := _json_object_at(page, brace)):
            return obj
        idx = page.find('"shortcode_media":', idx + 1)
    return None


def _best_image(node: dict) -> str | None:
    resources = node.get("display_resources") or []
    best = max(
        (r for r in resources if isinstance(r, dict) and r.get("src")),
        key=lambda r: r.get("config_width") or 0,
        default=None,
    )
    return (best or {}).get("src") or node.get("display_url")


def _item(node: dict) -> EmbedItem | None:
    if node.get("is_video") and node.get("video_url"):
        return EmbedItem(node["video_url"], "video")
    src = _best_image(node)
    return EmbedItem(src, "image") if src else None


def parse_embed(page: str) -> list[EmbedItem]:
    """Media items of an embed page, best resolution first, carousel in order. [] when the
    page has none (login wall, removed post, private)."""
    media = _media_json(page)
    if media:
        edges = (media.get("edge_sidecar_to_children") or {}).get("edges") or []
        nodes = [e.get("node") for e in edges if isinstance(e, dict)] or [media]
        items = [it for n in nodes if isinstance(n, dict) and (it := _item(n))]
        if items:
            return items[:MAX_ITEMS]
    # No JSON: the page still renders the first item as a plain <img> / <video>.
    if m := _VIDEO_RE.search(page):
        return [EmbedItem(html.unescape(m.group(1)), "video")]
    if m := _IMG_RE.search(page):
        return [EmbedItem(html.unescape(m.group(1) or m.group(2)), "image")]
    return []


# ---- fetching ---------------------------------------------------------------------------


def _ext(item: EmbedItem, content_type: str | None) -> str:
    path = urlsplit(item.url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".heic"):
        if path.endswith(ext):
            return ".jpg" if ext in (".jpeg", ".heic") else ext
    if content_type:
        if "mp4" in content_type:
            return ".mp4"
        if "png" in content_type:
            return ".png"
        if "webp" in content_type:
            return ".webp"
    return ".mp4" if item.kind == "video" else ".jpg"


async def _fetch_html(target: str, settings: Settings, what: str, headers: dict) -> str:
    timeout = aiohttp.ClientTimeout(total=settings.extract_timeout)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout, headers=headers) as session,
            session.get(target, proxy=settings.download_proxy, allow_redirects=True) as resp,
        ):
            if resp.status in (401, 403):
                raise ExtractError(ErrorCode.LOGIN_REQUIRED, f"{what} {resp.status}")
            if resp.status == 404:
                raise ExtractError(ErrorCode.PRIVATE, f"{what} 404")
            if resp.status == 429:
                raise ExtractError(ErrorCode.RATE_LIMITED, f"{what} 429")
            if resp.status >= 400:
                raise ExtractError(ErrorCode.SOURCE_DOWN, f"{what} {resp.status}")
            if "/accounts/login" in str(resp.url):
                raise ExtractError(ErrorCode.LOGIN_REQUIRED, f"{what} redirected to login")
            return await resp.text(errors="replace")
    except (aiohttp.ClientError, TimeoutError) as e:
        raise ExtractError(ErrorCode.SOURCE_DOWN, f"{what} fetch: {e}") from e


async def fetch_embed_page(post_url: str, settings: Settings) -> str:
    target = embed_url(post_url)
    if not target:
        raise ExtractError(ErrorCode.UNSUPPORTED_URL, post_url)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    return await _fetch_html(target, settings, "embed", headers)


# ---- the post page's Open Graph tags ----------------------------------------------------

# What a plain logged-out GET of instagram.com/p/<id>/ gets from exits where the embed page
# is a shell: the server-rendered page with og:image = the full-size photo (verified live).
# No browser impersonation: a plain, honest client UA is what was tested.
PAGE_UA = "Mozilla/5.0 (compatible; media-downloader-bot/1.0)"
_OG_RE = re.compile(
    r'<meta\s+(?:[^>]*?\s)?(?:property|name)="(og:[a-z:_]+)"\s+content="([^"]*)"|'
    r'<meta\s+(?:[^>]*?\s)?content="([^"]*)"\s+(?:property|name)="(og:[a-z:_]+)"',
    re.IGNORECASE,
)
_MEDIA_HOST_RE = re.compile(r"^https://[a-z0-9.-]*(?:cdninstagram\.com|fbcdn\.net)/", re.IGNORECASE)
_CAROUSEL_HINT_RE = re.compile(r"carousel_media_count|edge_sidecar_to_children|GraphSidecar")


def og_tags(page: str) -> dict[str, str]:
    """First value of each og:* meta tag, HTML-unescaped."""
    tags: dict[str, str] = {}
    for m in _OG_RE.finditer(page):
        key, value = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        tags.setdefault(key.lower(), html.unescape(value))
    return tags


def parse_post_page(page: str) -> tuple[list[EmbedItem], bool]:
    """Media from the post page's Open Graph tags, plus whether the post looks like a
    carousel (then og:image is only its first item). [] when the page is a login wall."""
    tags = og_tags(page)
    video = tags.get("og:video:secure_url") or tags.get("og:video")
    image = tags.get("og:image")
    items: list[EmbedItem] = []
    if video and _MEDIA_HOST_RE.match(video):
        items.append(EmbedItem(video, "video"))
    elif (
        image
        and _MEDIA_HOST_RE.match(image)
        and tags.get("og:type", "").lower()
        in (
            "article",
            "photo",
            "instapp:photo",
            "video",
            "video.other",
            "",
        )
    ):
        items.append(EmbedItem(image, "image"))
    return items, bool(_CAROUSEL_HINT_RE.search(page))


def post_page_url(post_url: str) -> str | None:
    m = _SHORTCODE_RE.search(post_url)
    return f"https://www.instagram.com/p/{m.group(1)}/" if m else None


async def fetch_post_page(post_url: str, settings: Settings) -> str:
    target = post_page_url(post_url)
    if not target:
        raise ExtractError(ErrorCode.UNSUPPORTED_URL, post_url)
    return await _fetch_html(target, settings, "post page", {"User-Agent": PAGE_UA})


async def og_download(post_url: str, dest: Path, settings: Settings) -> list[Path]:
    """Cookie-less fallback #1: the post page's og:image / og:video. Covers single photos
    fully; for carousels it is the first item only (logged)."""
    page = await fetch_post_page(post_url, settings)
    items, carousel = parse_post_page(page)
    if not items:
        log.info("og_no_media", url=post_url, bytes=len(page), title=_title(page))
        raise ExtractError(ErrorCode.LOGIN_REQUIRED, "post page has no og:image (login wall)")
    if carousel:
        log.info("og_first_item_only", url=post_url, hint="carousel; rest needs a session")
    await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=True)
    return await download_items(items, dest, settings)


def _title(page: str) -> str:
    m = re.search(r"<title>([^<]{0,80})", page, re.IGNORECASE)
    return html.unescape(m.group(1)).strip() if m else ""


async def download_items(items: list[EmbedItem], dest: Path, settings: Settings) -> list[Path]:
    """Stream each CDN URL into dest with the size cap enforced while reading."""
    timeout = aiohttp.ClientTimeout(total=settings.download_timeout)
    headers = {"User-Agent": USER_AGENT, "Referer": "https://www.instagram.com/"}
    out: list[Path] = []
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for i, item in enumerate(items[:MAX_ITEMS], start=1):
                async with session.get(item.url, proxy=settings.download_proxy) as resp:
                    if resp.status != 200:
                        raise ExtractError(ErrorCode.SOURCE_DOWN, f"cdn {resp.status}")
                    declared = resp.content_length or 0
                    if declared > settings.max_file_bytes:
                        raise ExtractError(ErrorCode.TOO_LARGE, f"{declared} B")
                    path = dest / f"{i:02d}{_ext(item, resp.headers.get('Content-Type'))}"
                    size = 0
                    with path.open("wb") as fh:
                        async for chunk in resp.content.iter_chunked(256 * 1024):
                            size += len(chunk)
                            if size > settings.max_file_bytes:
                                raise ExtractError(ErrorCode.TOO_LARGE, f">{size} B")
                            fh.write(chunk)
                    out.append(path)
    except (aiohttp.ClientError, TimeoutError) as e:
        raise ExtractError(ErrorCode.SOURCE_DOWN, f"cdn fetch: {e}") from e
    return out


async def embed_download(post_url: str, dest: Path, settings: Settings) -> list[Path]:
    """Full fallback: embed page -> media URLs -> files. Raises ExtractError; LOGIN_REQUIRED
    when the page is served but carries no media (what a login-walled post looks like)."""
    page = await fetch_embed_page(post_url, settings)
    items = parse_embed(page)
    if not items:
        # From data-center / VPN IPs Instagram answers 200 with a ~500 KB generic shell
        # (login / consent markers, no media at all) for every post. Cookies are the fix.
        log.info(
            "embed_shell_no_media",
            url=post_url,
            bytes=len(page),
            login_markers="accounts/login" in page or "UnavailableToast" in page,
        )
        raise ExtractError(ErrorCode.LOGIN_REQUIRED, "embed page is a login shell, no media")
    await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=True)
    return await download_items(items, dest, settings)
