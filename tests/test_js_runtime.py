"""When deno (yt-dlp's JS challenge solver) dies, the error must say so, not "no format"."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from core.contracts import ErrorCode, Quality
from core.extractors import YtdlpLog, ytdlp_download
from core.worker import ExtractError


@pytest.fixture
def fake_ytdlp(monkeypatch):
    """A yt_dlp whose extraction warns about the JS runtime and then finds no format,
    which is exactly what an OOM-killed deno looks like from the outside."""
    state = {"warn": True}

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
            logger = self.opts.get("logger")
            if state["warn"] and logger:
                logger.warning(
                    "[youtube] x: Signature solving failed: deno exited with code 137; "
                    "only images are available for download"
                )
            raise DownloadError("ERROR: [youtube] x: Requested format is not available")

    mod = types.ModuleType("yt_dlp")
    mod.YoutubeDL = YoutubeDL
    mod.utils = types.SimpleNamespace(DownloadError=DownloadError, parse_bytes=lambda s: 1)
    monkeypatch.setitem(sys.modules, "yt_dlp", mod)
    return state


async def test_dead_js_runtime_is_explained(fake_ytdlp, settings, tmp_path: Path) -> None:
    with pytest.raises(ExtractError) as e:
        await ytdlp_download("https://youtu.be/x", tmp_path / "j", Quality.P360, settings)
    assert e.value.code is ErrorCode.UNKNOWN  # never PRIVATE
    assert "JS challenge solver failed" in e.value.detail
    assert "mem_limit" in e.value.detail and "deno exited with code 137" in e.value.detail


async def test_plain_no_format_keeps_the_original_message(fake_ytdlp, settings, tmp_path) -> None:
    fake_ytdlp["warn"] = False
    with pytest.raises(ExtractError) as e:
        await ytdlp_download("https://youtu.be/x", tmp_path / "j", Quality.P360, settings)
    assert e.value.detail.endswith("Requested format is not available")
    assert "JS challenge" not in e.value.detail


def test_log_collector_only_keeps_js_related_warnings() -> None:
    log = YtdlpLog()
    log.debug("[download] 10%")
    log.warning("[youtube] unable to download thumbnail")
    assert not log.js_runtime_failed
    log.warning("[youtube] n challenge solving failed: no supported JavaScript runtime")
    assert log.js_runtime_failed and "JavaScript runtime" in log.last_js_warning
