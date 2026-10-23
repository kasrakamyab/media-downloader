from __future__ import annotations

from core.contracts import ErrorCode, FormatOption, Quality
from core.extractors import formats_from_info
from core.worker import ExtractError, run_probe

MB = 1024 * 1024


def _f(fid, height=None, vcodec="avc1", acodec="none", filesize=None, tbr=None, abr=None):
    return {
        "format_id": fid,
        "height": height,
        "vcodec": vcodec,
        "acodec": acodec,
        "filesize": filesize,
        "tbr": tbr,
        "abr": abr,
    }


def test_formats_from_info_builds_menu() -> None:
    info = {
        "duration": 100,
        "formats": [
            _f("140", vcodec="none", acodec="mp4a", filesize=2 * MB, abr=128),
            _f("160", height=144, filesize=1 * MB),
            _f("134", height=360, filesize=5 * MB, tbr=400),
            _f("135", height=480, filesize=9 * MB),
            _f("136", height=720, filesize=20 * MB),
            _f("137", height=1080, filesize=50 * MB),
            _f("18", height=360, acodec="mp4a", filesize=6 * MB, tbr=500),  # progressive
        ],
    }
    menu = formats_from_info(info)
    assert [o.quality for o in menu] == [
        Quality.P1080,
        Quality.P720,
        Quality.P480,
        Quality.P360,
        Quality.AUDIO,
    ]
    by_q = {o.quality: o for o in menu}
    assert by_q[Quality.P1080].size_bytes == 52 * MB  # video-only + audio
    assert by_q[Quality.P360].size_bytes == 6 * MB  # progressive has audio: nothing added
    assert by_q[Quality.AUDIO].size_bytes == 2 * MB
    assert by_q[Quality.P720].label == "720p · 22 MB"
    assert by_q[Quality.AUDIO].label == "Audio only · 2.0 MB"


def test_formats_dedupe_rungs_and_estimate_size() -> None:
    info = {
        "duration": 60,
        "formats": [
            _f("a", vcodec="none", acodec="opus", tbr=100),
            _f("v", height=720, tbr=1000),  # no filesize -> estimate from tbr
        ],
    }
    menu = formats_from_info(info)
    assert [o.quality for o in menu] == [Quality.P1080, Quality.AUDIO]  # 720p max: one video rung
    video = menu[0]
    assert video.height == 720
    assert video.size_bytes == int(1100 * 1000 / 8 * 60)
    assert FormatOption(quality=Quality.P480).label == "480p"


def test_formats_from_empty() -> None:
    assert formats_from_info({}) == []


async def test_run_probe_never_raises(settings) -> None:
    async def ok(url, settings):
        return [FormatOption(quality=Quality.P720, size_bytes=1)], "title"

    async def fails(url, settings):
        raise ExtractError(ErrorCode.PRIVATE, "x")

    async def crashes(url, settings):
        raise RuntimeError("boom")

    r = await run_probe("u", prober=ok, settings=settings)
    assert r.formats[0].quality is Quality.P720 and r.title == "title" and r.error_code is None
    assert (await run_probe("u", prober=fails, settings=settings)).error_code is ErrorCode.PRIVATE
    assert (await run_probe("u", prober=crashes, settings=settings)).error_code is ErrorCode.UNKNOWN
