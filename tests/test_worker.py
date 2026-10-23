from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from core.cache import FileCache
from core.contracts import ErrorCode, Job, Platform, Quality
from core.extractors import classify, ladder_for, ytdlp_format
from core.limits import UserLimits
from core.telegram import FakeSender
from core.worker import ExtractError, collect_files, janitor_sweep, run_job


def make_job(url: str = "https://www.youtube.com/watch?v=dQw4w9WgXcQ") -> Job:
    return Job(chat_id=10, user_id=20, message_id=30, url=url, platform=Platform.YOUTUBE)


async def _run(job, extractor, redis, settings, sender=None):
    sender = sender or FakeSender()
    limits = UserLimits(redis)
    await limits.try_acquire(job.user_id)
    result = await run_job(
        job,
        extractor=extractor,
        sender=sender,
        cache=FileCache(redis),
        limits=limits,
        redis=redis,
        settings=settings,
    )
    return result, sender


async def test_success_path_uploads_caches_and_cleans(redis, settings) -> None:
    job = make_job()
    seen: dict[str, Path] = {}

    async def extractor(job, dest, settings):
        seen["dest"] = dest
        f = dest / "video.mp4"
        f.write_bytes(b"x" * 100)
        return [f]

    result, sender = await _run(job, extractor, redis, settings)
    assert result.status == "ok" and not result.cached
    assert result.file_ids == ["fake-file-id-1"]
    assert sender.sent_files[0][0] == 10
    assert sender.finished == [(10, 30, None)]
    assert not seen["dest"].exists()  # temp folder deleted
    assert await FileCache(redis).get(job.url, Quality.BEST) == ["fake-file-id-1"]
    assert await redis.get("limits:active:20") is None  # slot released
    assert await redis.hget("stats:youtube", "ok") == b"1"


async def test_second_request_served_from_cache(redis, settings) -> None:
    job = make_job()
    calls = 0

    async def extractor(job, dest, settings):
        nonlocal calls
        calls += 1
        f = dest / "a.jpg"
        f.write_bytes(b"x")
        return [f]

    await _run(job, extractor, redis, settings)
    result, sender = await _run(make_job(), extractor, redis, settings)
    assert calls == 1
    assert result.cached and result.file_ids == ["fake-file-id-1"]
    assert sender.sent_cached == [(10, ["fake-file-id-1"], None)]
    assert sender.sent_files == []


async def test_extract_error_reports_code(redis, settings) -> None:
    async def extractor(job, dest, settings):
        raise ExtractError(ErrorCode.PRIVATE, "login wall")

    result, sender = await _run(make_job(), extractor, redis, settings)
    assert result.status == "error" and result.error_code is ErrorCode.PRIVATE
    assert sender.finished[0][2].startswith("❌")
    assert "private" in sender.finished[0][2]
    assert await redis.get("limits:active:20") is None


async def test_too_large(redis, settings) -> None:
    async def extractor(job, dest, settings):
        f = dest / "big.mp4"
        f.write_bytes(b"x" * (settings.max_file_bytes + 1))
        return [f]

    result, _ = await _run(make_job(), extractor, redis, settings)
    assert result.error_code is ErrorCode.TOO_LARGE


async def test_unexpected_exception_is_unknown(redis, settings) -> None:
    async def extractor(job, dest, settings):
        raise RuntimeError("boom")

    result, _ = await _run(make_job(), extractor, redis, settings)
    assert result.error_code is ErrorCode.UNKNOWN


async def test_timeout_is_source_down(redis, settings) -> None:
    async def extractor(job, dest, settings):
        await asyncio.sleep(10)
        return []

    result, _ = await _run(make_job(), extractor, redis, settings)
    assert result.error_code is ErrorCode.SOURCE_DOWN


async def test_blocked_url_refused(redis, settings) -> None:
    job = make_job()
    await FileCache(redis).block(job.url)
    called = False

    async def extractor(job, dest, settings):
        nonlocal called
        called = True
        return []

    result, _ = await _run(job, extractor, redis, settings)
    assert result.error_code is ErrorCode.UNSUPPORTED_URL and not called


def test_janitor(tmp_path: Path) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    stale = time.time() - 3600
    os.utime(old, (stale, stale))
    assert janitor_sweep(tmp_path, max_age_s=600) == 1
    assert not old.exists() and new.exists()
    assert janitor_sweep(tmp_path / "missing") == 0


def test_collect_files_skips_leftovers(tmp_path: Path) -> None:
    (tmp_path / "a.mp4").write_bytes(b"1")
    (tmp_path / "a.mp4.part").write_bytes(b"1")
    (tmp_path / "a.info.json").write_bytes(b"1")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.jpg").write_bytes(b"1")
    assert [p.name for p in collect_files(tmp_path)] == ["a.mp4", "b.jpg"]


def test_ladder_and_format() -> None:
    assert ladder_for(Quality.BEST) == [Quality.P720, Quality.P480, Quality.P360, Quality.AUDIO]
    assert ladder_for(Quality.P480) == [Quality.P480, Quality.P360, Quality.AUDIO]
    assert ladder_for(Quality.P1080)[0] is Quality.P1080
    assert ladder_for(Quality.AUDIO) == [Quality.AUDIO]
    assert "height<=360" in ytdlp_format(Quality.P360)
    assert ytdlp_format(Quality.AUDIO).startswith("ba")


def test_classify() -> None:
    assert classify("ERROR: Unsupported URL: https://x") is ErrorCode.UNSUPPORTED_URL
    assert classify("HTTP Error 429: Too Many Requests") is ErrorCode.RATE_LIMITED
    assert classify("Private video. Sign in if you've been granted access") is ErrorCode.PRIVATE
    assert classify("Connection timed out") is ErrorCode.SOURCE_DOWN
    assert classify("Unable to connect to proxy: Tunnel failed: 403") is ErrorCode.SOURCE_DOWN
    assert classify("HTTP Error 503: Service Unavailable") is ErrorCode.SOURCE_DOWN
    assert classify("HTTP Error 404: Not Found") is ErrorCode.PRIVATE
    assert classify("weird") is ErrorCode.UNKNOWN
