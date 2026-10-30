"""Captions: builder, length limit, escaping, mappings, cache round-trip, sender fallback."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest

from core.cache import FileCache, cache_key
from core.captions import (
    CAPTION_LIMIT,
    MediaMeta,
    build_caption,
    compact,
    from_gallerydl,
    from_og,
    from_ytdlp,
    visible_len,
)
from core.contracts import Job, Platform, Quality
from core.limits import UserLimits
from core.telegram import FakeSender, TelegramSender
from core.worker import Extracted, run_job

FULL = MediaMeta(
    title="LOVES doing this every single time! 😾 cr: 虎子不是闹闹",
    url="https://www.youtube.com/shorts/SisreHxY2Xg",
    uploader="Pawcsu",
    uploader_url="https://www.youtube.com/@Pawcsu",
    date="2026-10-13",
    duration_s=10,
    views=6_800_000,
    likes=119_500,
    comments=769,
    followers=60_900,
    description=(
        "LOVES doing this every single time! 😾\ncr: 虎子不是闹闹\n"
        "Welcome to the world of Pawcsu 😺✨"
    ),
    format="mp4 480p",
    filesize=673 * 1024,
)


# ---- formatting ---------------------------------------------------------------------------


def test_compact_numbers() -> None:
    assert compact(769) == "769"
    assert compact(6_800_000) == "6.8M"
    assert compact(119_500) == "119.5K"
    assert compact(60_900) == "60.9K"
    assert compact(1_000) == "1K" and compact(150_000) == "150K"
    assert compact(2_300_000_000) == "2.3B"
    assert compact(None) is None


def test_full_caption_layout() -> None:
    cap = build_caption(FULL, "full")
    lines = cap.split("\n")
    assert lines[0] == (
        '<a href="https://www.youtube.com/shorts/SisreHxY2Xg">'
        "LOVES doing this every single time! 😾 cr: 虎子不是闹闹</a>"
    )
    assert lines[1] == (
        "Oct 13, 2026 · [0:10] · 6.8M views · 119.5K likes · 769 comments · "
        '<a href="https://www.youtube.com/@Pawcsu">Pawcsu</a> 60.9K subscribers · '
        "<i>mp4 480p [673 KB]</i>"
    )
    assert lines[2] == ""
    assert lines[3].startswith("<blockquote expandable>LOVES doing this every single time!")
    assert cap.endswith("Pawcsu 😺✨</blockquote>")
    assert visible_len(cap) <= CAPTION_LIMIT


def test_short_and_off_modes() -> None:
    short = build_caption(FULL, "short")
    assert short == (
        '<a href="https://www.youtube.com/shorts/SisreHxY2Xg">'
        "LOVES doing this every single time! 😾 cr: 虎子不是闹闹</a>"
    )
    assert build_caption(FULL, "off") is None
    assert build_caption(None, "full") is None


def test_partial_metadata_drops_missing_fields() -> None:
    cap = build_caption(MediaMeta(title="Just a title", url="https://x/y"), "full")
    assert cap == '<a href="https://x/y">Just a title</a>'
    cap = build_caption(MediaMeta(uploader="kasra", description="Meow"), "full")
    assert cap == "kasra\n\n<blockquote expandable>Meow</blockquote>"
    cap = build_caption(MediaMeta(url="https://x/y", views=5), "full")
    assert cap == '<a href="https://x/y">Source</a>\n5 views'


def test_escaping() -> None:
    meta = MediaMeta(
        title='<b>Tom & "Jerry"</b>',
        url="https://x/y?a=1&b=2",
        uploader="A<B",
        description="1 < 2 && 3 > 2 <script>",
    )
    cap = build_caption(meta, "full")
    assert (
        '<a href="https://x/y?a=1&amp;b=2">&lt;b&gt;Tom &amp; &quot;Jerry&quot;&lt;/b&gt;</a>'
        in cap
    )
    assert "A&lt;B" in cap and "&lt;script&gt;" in cap
    assert "<script>" not in cap and "<b>" not in cap


def test_note_comes_first_and_is_escaped() -> None:
    cap = build_caption(
        MediaMeta(title="T", url="https://x/y"), "full", note="⚠️ Preview only <crop>"
    )
    assert cap.startswith("⚠️ Preview only &lt;crop&gt;\n<a href=")
    assert build_caption(None, "full", note="⚠️ Preview") == "⚠️ Preview"
    assert build_caption(FULL, "off", note="⚠️ Preview") == "⚠️ Preview"


@pytest.mark.parametrize(
    "text",
    [
        "این یک متن طولانی فارسی است که بارها تکرار می‌شود تا از حد مجاز بگذرد. " * 40,
        "这是一个很长的中文描述，用来测试字幕长度限制是否按预期工作。" * 60,
        "emoji 😾🐱✨ repeated " * 120,
    ],
)
def test_long_description_is_trimmed_under_the_limit(text: str) -> None:
    meta = FULL.model_copy(update={"description": text})
    cap = build_caption(meta, "full")
    assert visible_len(cap) <= CAPTION_LIMIT
    assert cap.endswith("…</blockquote>")
    assert cap.split("\n")[0].startswith("<a href=")  # head untouched


def test_huge_title_is_trimmed_and_description_dropped() -> None:
    meta = MediaMeta(title="عنوان " * 400, url="https://x/y", description="desc " * 100)
    cap = build_caption(meta, "full")
    assert visible_len(cap) <= CAPTION_LIMIT and "<blockquote" not in cap
    assert cap.endswith("…")


def test_visible_len_counts_utf16_units_without_tags() -> None:
    assert visible_len('<a href="https://x">ab</a>') == 2
    assert visible_len("😾") == 2  # astral plane: two UTF-16 units, as Telegram counts
    assert visible_len("&amp;&lt;") == 2


# ---- mappings -----------------------------------------------------------------------------


def test_from_ytdlp() -> None:
    info = {
        "title": "T",
        "webpage_url": "https://www.youtube.com/watch?v=x",
        "channel": "Pawcsu",
        "channel_url": "https://www.youtube.com/@Pawcsu",
        "upload_date": "20261013",
        "duration": 10.4,
        "view_count": 6800000,
        "like_count": 119500,
        "comment_count": 769,
        "channel_follower_count": 60900,
        "description": "d",
        "ext": "mp4",
        "height": 480,
    }
    m = from_ytdlp(info, 689_152)
    assert m.date == "2026-10-13" and m.duration_s == 10 and m.format == "mp4 480p"
    assert m.filesize == 689_152 and m.uploader == "Pawcsu" and m.followers == 60900
    audio = from_ytdlp({"ext": "m4a", "acodec": "mp4a", "timestamp": 1760313600})
    assert audio.format == "m4a audio" and audio.date == "2025-10-13"
    assert from_ytdlp({}) == MediaMeta()


def test_from_gallerydl_instagram_and_pinterest() -> None:
    ig = from_gallerydl(
        {
            "username": "kasra",
            "fullname": "Kasra",
            "description": "Meow",
            "date": "2026-10-29 10:00:00",
            "likes": 4,
            "post_url": "https://www.instagram.com/p/DfEciETonwc/",
        },
        "instagram",
    )
    assert ig.uploader == "Kasra" and ig.uploader_url == "https://www.instagram.com/kasra/"
    assert ig.date == "2026-10-29" and ig.likes == 4 and ig.description == "Meow"
    pin = from_gallerydl(
        {
            "title": "Cute & sweet my kitty",
            "description": "",
            "pinner": {"username": "kasra21", "full_name": "Kasra"},
            "created_at": "Wed, 29 Oct 2026 01:00:00 +0000",
        },
        "pinterest",
        "https://www.pinterest.com/pin/1/",
    )
    assert pin.title == "Cute & sweet my kitty" and pin.uploader == "Kasra"
    assert pin.uploader_url == "https://www.pinterest.com/kasra21/" and pin.url.endswith("/pin/1/")
    assert pin.date is None  # RFC-2822 dates are not parsed; field dropped, not wrong


def test_from_og() -> None:
    m = from_og(
        {"og:title": 'Kasra on Instagram: "Meow"', "og:description": "4 likes - Meow"},
        "https://www.instagram.com/p/DfEciETonwc/",
    )
    assert m.uploader == "Kasra" and m.title == 'Kasra on Instagram: "Meow"'
    assert m.description == "4 likes - Meow"


# ---- cache round-trip and worker ----------------------------------------------------------


async def test_cache_keeps_meta_and_note(redis) -> None:
    cache = FileCache(redis)
    await cache.set("u", Quality.BEST, ["id1"], FULL, "⚠️ Preview")
    entry = await cache.get_entry("u", Quality.BEST)
    assert entry.file_ids == ["id1"] and entry.meta == FULL and entry.note == "⚠️ Preview"
    assert await cache.get("u", Quality.BEST) == ["id1"]
    # entries written before captions existed (a bare list) still work
    await redis.set(cache_key("old", Quality.BEST), json.dumps(["x"]))
    old = await cache.get_entry("old", Quality.BEST)
    assert old.file_ids == ["x"] and old.meta is None


def _job(captions: str = "full", **kw) -> Job:
    base = dict(chat_id=1, user_id=2, message_id=3, url="https://x/c", platform=Platform.YOUTUBE)
    return Job(**{**base, "captions": captions, **kw})


async def _run(redis, settings, job, extractor, sender=None):
    sender = sender or FakeSender()
    limits = UserLimits(redis)
    await limits.try_acquire(job.user_id)
    await run_job(
        job,
        extractor=extractor,
        sender=sender,
        cache=FileCache(redis),
        limits=limits,
        redis=redis,
        settings=settings,
    )
    return sender


async def test_worker_captions_and_cached_resend(redis, settings) -> None:
    async def extractor(job, dest, settings):
        f = dest / "v.mp4"
        f.write_bytes(b"x" * 10)
        return Extracted([f], meta=FULL.model_copy(update={"filesize": None}))

    sender = await _run(redis, settings, _job(), extractor)
    caption = sender.sent_files[0][2]
    assert caption.startswith('<a href="https://www.youtube.com/shorts/SisreHxY2Xg">')
    assert "[0 KB]" in caption  # filesize measured from the actual file when unknown

    # second request: cached, same caption rebuilt from the stored metadata
    sender2 = await _run(redis, settings, _job(), extractor)
    assert sender2.sent_cached and sender2.sent_cached[0][2] == caption

    # a chat with captions off gets the same file without text
    sender3 = await _run(redis, settings, _job(captions="off"), extractor)
    assert sender3.sent_cached[0][2] is None
    # short mode from the same cache entry
    sender4 = await _run(redis, settings, _job(captions="short"), extractor)
    assert sender4.sent_cached[0][2] == build_caption(FULL, "short")


async def test_preview_note_stays_first(redis, settings) -> None:
    async def extractor(job, dest, settings):
        f = dest / "01.jpg"
        f.write_bytes(b"x")
        return Extracted(
            [f],
            caption="⚠️ Preview only: crop",
            cacheable=False,
            meta=MediaMeta(title="Meow", url="https://i/p"),
        )

    sender = await _run(redis, settings, _job(url="https://x/p"), extractor)
    cap = sender.sent_files[0][2]
    assert cap.startswith("⚠️ Preview only: crop\n<a href=")


# ---- sender fallback when Telegram rejects the caption ------------------------------------


class _Bot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def _send(self, name, chat_id, *a, **kw):
        cap = kw.get("caption")
        if name == "send_media_group":
            cap = kw["media"][0].caption
        self.calls.append((name, cap))
        if cap and "boom" in cap:
            raise TelegramBadRequest(method=SimpleNamespace(), message="MEDIA_CAPTION_TOO_LONG")
        msg = SimpleNamespace(video=SimpleNamespace(file_id="fid"), photo=None, document=None)
        return [msg] * len(kw["media"]) if name == "send_media_group" else msg

    def __getattr__(self, name):
        if name.startswith("send_"):
            return lambda *a, **kw: self._send(name, *a, **kw)
        raise AttributeError(name)


async def test_rejected_caption_resends_without_it(tmp_path: Path) -> None:
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    bot = _Bot()
    sender = TelegramSender(bot)  # type: ignore[arg-type]
    assert await sender.send_files(1, [f], "boom caption") == ["fid"]
    assert [c for c in bot.calls] == [("send_video", "boom caption"), ("send_video", None)]

    bot.calls.clear()
    await sender.send_cached(1, ["fid"], "boom caption")
    assert bot.calls == [("send_document", "boom caption"), ("send_document", None)]

    # media groups: caption on the first item only, same fallback
    g = [tmp_path / "a.jpg", tmp_path / "b.jpg"]
    for p in g:
        p.write_bytes(b"x")
    bot.calls.clear()
    await sender.send_files(1, g, "boom caption")
    assert bot.calls == [("send_media_group", "boom caption"), ("send_media_group", None)]
