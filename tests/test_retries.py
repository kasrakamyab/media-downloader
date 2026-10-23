from __future__ import annotations

from core.breaker import Breaker
from core.cache import FileCache
from core.contracts import ErrorCode, Job, Platform
from core.limits import UserLimits
from core.telegram import FakeSender
from core.worker import ExtractError, run_job

_n = 0


def make_job() -> Job:
    global _n
    _n += 1  # distinct URL per job so a success doesn't serve the next run from cache
    return Job(chat_id=1, user_id=2, message_id=3, url=f"https://x/{_n}", platform=Platform.YOUTUBE)


async def _run(redis, settings, extractor, breaker=None):
    return await run_job(
        make_job(),
        extractor=extractor,
        sender=FakeSender(),
        cache=FileCache(redis),
        limits=UserLimits(redis),
        redis=redis,
        settings=settings,
        breaker=breaker,
    )


async def test_transient_error_is_retried_then_succeeds(redis, settings) -> None:
    settings.transient_retries = 2
    settings.retry_backoff = 0
    calls = 0

    async def flaky(job, dest, settings):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ExtractError(ErrorCode.SOURCE_DOWN, "blip")
        f = dest / "ok.mp4"
        f.write_bytes(b"x")
        return [f]

    result = await _run(redis, settings, flaky)
    assert result.status == "ok" and calls == 3


async def test_non_transient_error_is_not_retried(redis, settings) -> None:
    settings.transient_retries = 3
    settings.retry_backoff = 0
    calls = 0

    async def private(job, dest, settings):
        nonlocal calls
        calls += 1
        raise ExtractError(ErrorCode.PRIVATE, "nope")

    result = await _run(redis, settings, private)
    assert result.error_code is ErrorCode.PRIVATE and calls == 1


async def test_failures_feed_the_breaker(redis, settings) -> None:
    settings.transient_retries = 0
    breaker = Breaker(redis, threshold=2)

    async def down(job, dest, settings):
        raise ExtractError(ErrorCode.SOURCE_DOWN, "x")

    async def ok(job, dest, settings):
        f = dest / "ok.jpg"
        f.write_bytes(b"x")
        return [f]

    await _run(redis, settings, down, breaker)
    assert await breaker.failures(Platform.YOUTUBE) == 1
    await _run(redis, settings, ok, breaker)  # success resets the window
    assert await breaker.failures(Platform.YOUTUBE) == 0
    await _run(redis, settings, down, breaker)
    await _run(redis, settings, down, breaker)
    assert await breaker.is_open(Platform.YOUTUBE)
