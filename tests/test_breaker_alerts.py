from __future__ import annotations

import time

from core.breaker import Breaker, open_key
from core.contracts import Platform
from core.health import beat
from core.worker import flag_login_required
from gateway.alerts import check_once


async def test_breaker_opens_after_threshold(redis) -> None:
    b = Breaker(redis, threshold=3, window_s=600, open_for_s=900)
    assert not await b.record_failure(Platform.YOUTUBE)
    assert not await b.record_failure(Platform.YOUTUBE)
    assert not await b.is_open(Platform.YOUTUBE)
    assert await b.failures(Platform.YOUTUBE) == 2
    assert await b.record_failure(Platform.YOUTUBE)  # third one opens it
    assert await b.is_open(Platform.YOUTUBE)
    assert 800 < await redis.ttl(open_key(Platform.YOUTUBE)) <= 900
    assert await b.failures(Platform.YOUTUBE) == 0
    # other platforms untouched
    assert not await b.is_open(Platform.INSTAGRAM)
    # more failures while open do not report "opened" again
    for _ in range(3):
        opened = await b.record_failure(Platform.YOUTUBE)
    assert opened is False


async def test_success_clears_failure_window(redis) -> None:
    b = Breaker(redis, threshold=2)
    await b.record_failure(Platform.PINTEREST)
    await b.record_success(Platform.PINTEREST)
    assert not await b.record_failure(Platform.PINTEREST)
    assert not await b.is_open(Platform.PINTEREST)


async def test_old_failures_fall_out_of_window(redis) -> None:
    b = Breaker(redis, threshold=2, window_s=60)
    await redis.zadd("breaker:fail:youtube", {"old": time.time() - 120})
    assert not await b.record_failure(Platform.YOUTUBE)


async def test_reset(redis) -> None:
    b = Breaker(redis, threshold=1)
    await b.record_failure(Platform.YOUTUBE)
    await b.reset(Platform.YOUTUBE)
    assert not await b.is_open(Platform.YOUTUBE)


async def test_alerts_down_and_back(redis, settings) -> None:
    b = Breaker(redis, threshold=1)
    for p in Platform:
        await beat(redis, p, 60)
    assert await check_once(redis, b, settings) == []

    await b.record_failure(Platform.YOUTUBE)
    msgs = await check_once(redis, b, settings)
    assert msgs == ["🔴 YouTube is down: circuit breaker opened."]
    assert await check_once(redis, b, settings) == []  # no repeat

    await redis.delete("heartbeat:instagram")
    assert await check_once(redis, b, settings) == [
        "🔴 Instagram is down: worker heartbeat missing."
    ]

    await b.reset(Platform.YOUTUBE)
    await beat(redis, Platform.INSTAGRAM, 60)
    msgs = await check_once(redis, b, settings)
    assert sorted(msgs) == ["🟢 Instagram is back.", "🟢 YouTube is back."]


async def test_alert_login_required(redis, settings) -> None:
    b = Breaker(redis)
    for p in Platform:
        await beat(redis, p, 60)
    await flag_login_required(redis, Platform.INSTAGRAM, "HTTP redirect to login page")
    msgs = await check_once(redis, b, settings)
    assert len(msgs) == 1 and msgs[0].startswith("🔑 Instagram is asking for a login")
    assert "redirect to login page" in msgs[0]
    assert await check_once(redis, b, settings) == []  # flag consumed


async def test_alert_long_open(redis, settings) -> None:
    settings.alert_after_open_s = 0
    b = Breaker(redis, threshold=1)
    for p in Platform:
        await beat(redis, p, 60)
    await b.record_failure(Platform.PINTEREST)
    msgs = await check_once(redis, b, settings)
    assert msgs[0].startswith("🔴 Pinterest")
    assert msgs[1].startswith("⚠️ Pinterest breaker has been open")
    assert await check_once(redis, b, settings) == []
