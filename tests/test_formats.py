"""yt-dlp format selection, evaluated offline by yt-dlp itself against format lists shaped
like the real sources (a Pinterest HLS pin, a YouTube video)."""

from __future__ import annotations

import pytest

from core.contracts import ErrorCode, Quality
from core.extractors import classify, ytdlp_format, ytdlp_sort


def _v(fid, w, h, tbr, ext="mp4", acodec="none", protocol="https"):
    return {
        "format_id": fid,
        "ext": ext,
        "width": w,
        "height": h,
        "tbr": tbr,
        "vcodec": "avc1.64001f",
        "acodec": acodec,
        "protocol": protocol,
        "url": f"https://cdn/{fid}",
    }


def _a(fid, abr, ext="m4a"):
    return {
        "format_id": fid,
        "ext": ext,
        "abr": abr,
        "tbr": abr,
        "vcodec": "none",
        "acodec": "mp4a.40.2",
        "protocol": "https",
        "url": f"https://cdn/{fid}",
    }


# pin 670121619595748727 as reported live: video-only HLS renditions plus one audio-only
# stream whose ext is mp4 (not m4a); no muxed format at all.
PINTEREST = [
    _v("V_HLSV3_MOBILE-194", 360, 640, 194, protocol="m3u8_native"),
    _v("V_HLSV3_MOBILE-307", 480, 854, 307, protocol="m3u8_native"),
    _v("V_HLSV3_MOBILE-441", 540, 960, 441, protocol="m3u8_native"),
    _v("V_HLSV3_MOBILE-751", 720, 1280, 751, protocol="m3u8_native"),
    _a("V_HLSV3_MOBILE-audio1-1", 64, ext="mp4"),
]

YOUTUBE = [
    _v("160", 256, 144, 100),
    _v("134", 640, 360, 600),
    _v("135", 854, 480, 1000),
    _v("136", 1280, 720, 2500),
    _v("137", 1920, 1080, 4500),
    _v("18", 640, 360, 700, acodec="mp4a.40.2"),  # progressive
    _a("140", 128),
]

SHORT_PORTRAIT = [  # a 1080x1920 Short with the usual rungs
    _v("v360", 360, 640, 400),
    _v("v720", 720, 1280, 1800),
    _v("v1080", 1080, 1920, 3500),
    _a("140", 128),
]


def pick(formats: list[dict], quality: Quality) -> list[str]:
    """What yt-dlp would download for this rung: format ids of the chosen stream(s)."""
    yt_dlp = pytest.importorskip("yt_dlp")
    opts = {
        "format": ytdlp_format(quality),
        "format_sort": ytdlp_sort(quality),
        "quiet": True,
        "no_warnings": True,
        "simulate": True,
    }
    info = {
        "id": "x",
        "title": "t",
        "webpage_url": "https://example.com/x",
        "extractor": "generic",
        "extractor_key": "Generic",
        "formats": [dict(f) for f in formats],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        result = ydl.process_ie_result(info, download=False)
    chosen = result.get("requested_formats") or [result]
    return [f["format_id"] for f in chosen]


def test_pinterest_hls_pin_gets_video_plus_mp4_audio() -> None:
    # the live failure: nothing matched because the audio isn't m4a and nothing is muxed
    assert pick(PINTEREST, Quality.BEST) == ["V_HLSV3_MOBILE-751", "V_HLSV3_MOBILE-audio1-1"]
    assert pick(PINTEREST, Quality.P720) == ["V_HLSV3_MOBILE-751", "V_HLSV3_MOBILE-audio1-1"]
    assert pick(PINTEREST, Quality.P480) == ["V_HLSV3_MOBILE-307", "V_HLSV3_MOBILE-audio1-1"]
    assert pick(PINTEREST, Quality.P360) == ["V_HLSV3_MOBILE-194", "V_HLSV3_MOBILE-audio1-1"]
    assert pick(PINTEREST, Quality.AUDIO) == ["V_HLSV3_MOBILE-audio1-1"]


def test_portrait_video_is_capped_by_its_short_side() -> None:
    # [height<=720] used to pick 360x640 for a portrait video; res:720 picks 720x1280
    assert pick(SHORT_PORTRAIT, Quality.P720) == ["v720", "140"]
    assert pick(SHORT_PORTRAIT, Quality.P360) == ["v360", "140"]
    assert pick(SHORT_PORTRAIT, Quality.P1080) == ["v1080", "140"]


def test_youtube_rungs_still_prefer_mp4_and_m4a() -> None:
    assert pick(YOUTUBE, Quality.P720) == ["136", "140"]
    assert pick(YOUTUBE, Quality.P480) == ["135", "140"]
    assert pick(YOUTUBE, Quality.P1080) == ["137", "140"]
    assert pick(YOUTUBE, Quality.AUDIO) == ["140"]


def test_only_a_muxed_file_available() -> None:
    assert pick([_v("18", 640, 360, 700, acodec="mp4a.40.2")], Quality.P720) == ["18"]


def test_no_format_is_not_private() -> None:
    msg = (
        "ERROR: [Pinterest] 670121619595748727: Requested format is not available. "
        "Use --list-formats"
    )
    assert classify(msg) is ErrorCode.UNKNOWN
    # the generic "not available" wording still means the post itself
    assert classify("Video not available in your country") is ErrorCode.PRIVATE
