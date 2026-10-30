"""HLS muxed formats can't be size-capped by yt-dlp: prefer https pairs, and fall through
the ladder when a rung still comes back too big."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from core.captions import from_ytdlp
from core.contracts import ErrorCode, Quality
from core.extractors import ytdlp_download
from core.worker import ExtractError
from tests.test_formats import _a, _v, pick

MB = 1024 * 1024

# Short H1x8xHHG4GU as reported live: a muxed HLS 720x1280p60 with the fattest bitrate and
# only an approximate size, next to an https DASH pair with exact sizes.
SHORT_WITH_HLS = [
    _v("298", 720, 1280, 1500),  # https, video-only, 31.7 MiB exact
    _v("137", 1080, 1920, 3000),
    _v("300", 720, 1280, 2191, acodec="mp4a.40.2", protocol="m3u8_native"),  # muxed HLS
    _v("95", 720, 1280, 2100, acodec="mp4a.40.2", protocol="m3u8_native"),
    _a("140", 128),
]


def test_https_pair_beats_fatter_muxed_hls() -> None:
    assert pick(SHORT_WITH_HLS, Quality.P720) == ["298", "140"]
    assert pick(SHORT_WITH_HLS, Quality.BEST) == ["298", "140"]


def test_hls_only_source_still_works() -> None:
    hls_only = [f for f in SHORT_WITH_HLS if f["protocol"] == "m3u8_native"]
    assert pick(hls_only, Quality.P720) == ["300"]  # best muxed HLS when nothing else exists


@pytest.fixture
def fake_ytdlp(monkeypatch):
    """Writes a file whose size depends on the rung: the first rung comes back over the
    cap (an HLS download max_filesize couldn't stop), the next one fits."""
    sizes = {"720p": 60 * MB, "480p": 20 * MB, "360p": 5 * MB, "audio": 1 * MB}
    seen: list[str] = []

    class DownloadError(Exception):
        pass

    class YoutubeDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            first = self.opts["format_sort"][0]
            rung = "audio" if first == "abr" else first.split(":")[1] + "p"
            seen.append(rung)
            out = Path(self.opts["outtmpl"]).parent / "H1x8xHHG4GU.mp4"
            out.write_bytes(b"\0" * sizes[rung])
            return {"id": "H1x8xHHG4GU", "ext": "mp4", "width": 720, "height": 1280}

        def sanitize_info(self, info):
            return info

    mod = types.ModuleType("yt_dlp")
    mod.YoutubeDL = YoutubeDL
    mod.utils = types.SimpleNamespace(DownloadError=DownloadError, parse_bytes=lambda s: 1)
    monkeypatch.setitem(sys.modules, "yt_dlp", mod)
    return seen


async def test_oversized_rung_falls_through(fake_ytdlp, settings, tmp_path: Path) -> None:
    settings.max_file_mb = 48
    dest = tmp_path / "j"
    dest.mkdir()
    got = await ytdlp_download("https://youtu.be/H1x8xHHG4GU", dest, Quality.BEST, settings)
    assert fake_ytdlp == ["720p", "480p"]  # 720p came back at 60 MB: deleted, next rung
    assert [f.name for f in got.files] == ["H1x8xHHG4GU.mp4"]
    assert got.files[0].stat().st_size == 20 * MB
    assert got.meta.filesize == 20 * MB and got.meta.format == "mp4 720p"


async def test_every_rung_too_big_is_too_large(fake_ytdlp, settings, tmp_path: Path) -> None:
    settings.max_file_mb = 0  # nothing fits
    with pytest.raises(ExtractError) as e:
        await ytdlp_download("https://youtu.be/x", tmp_path / "j", Quality.BEST, settings)
    assert e.value.code is ErrorCode.TOO_LARGE
    assert fake_ytdlp == ["720p", "480p", "360p", "audio"]
    assert list((tmp_path / "j").iterdir()) == []  # nothing left behind


def test_caption_resolution_is_the_short_side_of_the_chosen_stream() -> None:
    info = {
        "ext": "mp4",
        "width": 1080,
        "height": 1920,  # the source's dimensions
        "requested_formats": [
            {"format_id": "95", "vcodec": "avc1", "width": 720, "height": 1280},
            {"format_id": "140", "vcodec": "none", "acodec": "mp4a"},
        ],
    }
    assert from_ytdlp(info).format == "mp4 720p"
    assert from_ytdlp({"ext": "mp4", "width": 1920, "height": 1080}).format == "mp4 1080p"
    assert from_ytdlp({"ext": "mp4", "height": 480}).format == "mp4 480p"
