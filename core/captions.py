"""Captions for delivered media, built from whatever metadata the extractor had.

Layout (HTML parse mode, every field optional, missing ones simply dropped):

    ⚠️ Preview only …                                  <- note, when the result is degraded
    <a href="source">Title</a>
    Oct 13, 2026 · [0:10] · 6.8M views · 119.5K likes · 769 comments · <a href="…">Channel</a>
    60.9K subscribers · mp4 480p [673 KB]

    <blockquote expandable>description…</blockquote>

Telegram caps captions at 1024 characters *after* entity parsing, counted in UTF-16 code
units; the description is trimmed first, then dropped, then the title is trimmed, so a send
never fails because of the caption.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import escape
from typing import Literal

from pydantic import BaseModel

CaptionMode = Literal["full", "short", "off"]
CAPTION_LIMIT = 1024  # Telegram's limit for media captions
ELLIPSIS = "…"


class MediaMeta(BaseModel):
    """Source metadata in a platform-neutral shape. Cached next to the file ids, so a cached
    re-send can build the same caption (in whatever mode that chat wants)."""

    title: str | None = None
    url: str | None = None  # the post / video page
    uploader: str | None = None
    uploader_url: str | None = None
    date: str | None = None  # ISO date "2026-10-13"
    duration_s: int | None = None
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    followers: int | None = None
    description: str | None = None
    format: str | None = None  # e.g. "mp4 480p"
    filesize: int | None = None


# ---- extractor mappings -----------------------------------------------------------------


def _int(v: object) -> int | None:
    try:
        return int(v) if v is not None else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _date_from_yyyymmdd(s: object) -> str | None:
    if isinstance(s, str) and re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return None


def _date_from_iso(s: object) -> str | None:
    if not isinstance(s, str) or len(s) < 10:
        return None
    return s[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s[:10]) else None


def _date_from_timestamp(ts: object) -> str | None:
    n = _int(ts)
    if not n:
        return None
    return datetime.fromtimestamp(n, UTC).date().isoformat()


def from_ytdlp(info: dict, filesize: int | None = None) -> MediaMeta:
    fmt = None
    if info.get("ext"):
        fmt = str(info["ext"])
        if info.get("height"):
            fmt += f" {int(info['height'])}p"
        elif info.get("abr") or info.get("acodec") not in (None, "none"):
            fmt += " audio"
    return MediaMeta(
        title=info.get("title") or None,
        url=info.get("webpage_url") or info.get("original_url") or None,
        uploader=info.get("channel") or info.get("uploader") or None,
        uploader_url=info.get("channel_url") or info.get("uploader_url") or None,
        date=_date_from_yyyymmdd(info.get("upload_date"))
        or _date_from_timestamp(info.get("timestamp")),
        duration_s=_int(info.get("duration")),
        views=_int(info.get("view_count")),
        likes=_int(info.get("like_count")),
        comments=_int(info.get("comment_count")),
        followers=_int(info.get("channel_follower_count")),
        description=(info.get("description") or None),
        format=fmt,
        filesize=filesize or _int(info.get("filesize")) or _int(info.get("filesize_approx")),
    )


def from_gallerydl(data: dict, platform: str, fallback_url: str | None = None) -> MediaMeta:
    """gallery-dl's per-file metadata (`--write-metadata`): Instagram and Pinterest keys."""
    if platform == "instagram":
        user = data.get("username") or (data.get("owner") or {}).get("username")
        return MediaMeta(
            title=None,
            url=data.get("post_url") or fallback_url,
            uploader=data.get("fullname") or user,
            uploader_url=f"https://www.instagram.com/{user}/" if user else None,
            date=_date_from_iso(data.get("date")),
            likes=_int(data.get("likes")),
            comments=_int(data.get("comments")),
            description=data.get("description") or None,
        )
    if platform == "pinterest":
        pinner = data.get("pinner") or {}
        user = pinner.get("username") if isinstance(pinner, dict) else None
        board = data.get("board") or {}
        return MediaMeta(
            title=data.get("title") or (board.get("name") if isinstance(board, dict) else None),
            url=fallback_url,
            uploader=(pinner.get("full_name") if isinstance(pinner, dict) else None) or user,
            uploader_url=f"https://www.pinterest.com/{user}/" if user else None,
            date=_date_from_iso(data.get("created_at")) or _date_from_iso(data.get("date")),
            likes=_int(data.get("reaction_counts", {}).get("1"))
            if isinstance(data.get("reaction_counts"), dict)
            else None,
            comments=_int(data.get("comment_count")),
            description=data.get("description") or None,
        )
    return MediaMeta(
        title=data.get("title") or None,
        url=fallback_url,
        description=data.get("description") or None,
    )


def from_og(tags: dict[str, str], url: str | None) -> MediaMeta:
    title = tags.get("og:title")
    # "Kasra on Instagram: "Meow"" -> uploader "Kasra", title/description "Meow"
    uploader = None
    m = re.match(r"^(.*?) on Instagram: ", title or "")
    if m:
        uploader = m.group(1).strip() or None
    return MediaMeta(
        title=title or None,
        url=url,
        uploader=uploader,
        description=tags.get("og:description") or None,
    )


# ---- formatting ---------------------------------------------------------------------------


def compact(n: int | None) -> str | None:
    if n is None:
        return None
    for unit, div in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if n >= div:
            v = n / div
            s = f"{v:.1f}".rstrip("0").rstrip(".")  # 6.8M, 119.5K, 150K
            return f"{s}{unit}"
    return str(n)


def human_size(nbytes: int | None) -> str | None:
    if nbytes is None:
        return None
    if nbytes >= 1024 * 1024:
        return f"{nbytes / (1024 * 1024):.1f} MB"
    return f"{max(nbytes, 0) // 1024} KB"


def duration_label(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def date_label(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%b %-d, %Y")
    except ValueError:
        return None


def visible_len(html: str) -> int:
    """Length as Telegram counts it: the text without tags, in UTF-16 code units."""
    text = re.sub(r"<[^>]+>", "", html)
    text = (
        text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")
    )
    return len(text.encode("utf-16-le")) // 2


def _trim(text: str, budget: int) -> str:
    """Cut `text` to at most `budget` UTF-16 units (minus the ellipsis), on a word edge."""
    if budget <= 1:
        return ""
    units = text.encode("utf-16-le")
    if len(units) // 2 <= budget:
        return text
    cut = units[: (budget - 1) * 2].decode("utf-16-le", errors="ignore")
    if " " in cut[-20:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip() + ELLIPSIS


def _link(text: str, url: str | None) -> str:
    return f'<a href="{escape(url, quote=True)}">{escape(text)}</a>' if url else escape(text)


def build_caption(
    meta: MediaMeta | None,
    mode: CaptionMode = "full",
    note: str | None = None,
    limit: int = CAPTION_LIMIT,
) -> str | None:
    """HTML caption, or None when there is nothing to say. Never longer than `limit`."""
    if mode == "off" or (meta is None and not note):
        return escape(note) if note else None
    meta = meta or MediaMeta()
    head: list[str] = []
    if note:
        head.append(escape(note))
    title = (meta.title or "").strip()
    if not title and meta.url and mode != "off":
        title = "Source"
    if title:
        head.append(_link(title, meta.url))
    if mode == "short":
        return _fit(head, None, limit)

    facts: list[str] = []
    for value in (
        date_label(meta.date),
        f"[{duration_label(meta.duration_s)}]" if meta.duration_s is not None else None,
        f"{compact(meta.views)} views" if meta.views is not None else None,
        f"{compact(meta.likes)} likes" if meta.likes is not None else None,
        f"{compact(meta.comments)} comments" if meta.comments is not None else None,
    ):
        if value:
            facts.append(escape(value))
    if meta.uploader:
        who = _link(meta.uploader, meta.uploader_url)
        if meta.followers is not None:
            who += f" {escape(compact(meta.followers) or '')} subscribers"
        facts.append(who)
    tech = " ".join(
        x for x in (meta.format, f"[{human_size(meta.filesize)}]" if meta.filesize else None) if x
    )
    if tech:
        facts.append(f"<i>{escape(tech)}</i>")
    if facts:
        head.append(" · ".join(facts))

    description = (meta.description or "").strip()
    if (
        description
        and title
        and description.split("\n")[0].strip() == title
        and len(description) <= len(title) + 2
    ):
        description = ""  # description is just the title again
    return _fit(head, description or None, limit)


def _fit(head: list[str], description: str | None, limit: int) -> str | None:
    """Assemble within the limit: trim the description, then drop it, then trim the title."""
    base = "\n".join(head)
    if description:
        room = limit - visible_len(base) - 2  # the blank line
        if room > 20:
            desc = _trim(description, room)
            candidate = f"{base}\n\n<blockquote expandable>{escape(desc)}</blockquote>"
            if visible_len(candidate) <= limit:
                return candidate
    if visible_len(base) <= limit:
        return base or None
    # Even the head is too long (huge title): trim line by line from the end.
    lines = list(head)
    while lines and visible_len("\n".join(lines)) > limit:
        last = lines[-1]
        plain = re.sub(r"<[^>]+>", "", last)
        budget = limit - visible_len("\n".join(lines[:-1])) - (1 if len(lines) > 1 else 0)
        if budget > 10:
            lines[-1] = escape(_trim(plain, budget))
            break
        lines.pop()
    return "\n".join(lines) or None
