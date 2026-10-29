from __future__ import annotations

from core.cache import FileCache, cache_key
from core.contracts import Platform, Quality, Result
from core.health import beat, is_alive, record_result, stats
from core.limits import UserLimits


async def test_cache_roundtrip(redis) -> None:
    cache = FileCache(redis, ttl_days=1)
    assert await cache.get("u", Quality.BEST) is None
    await cache.set("u", Quality.BEST, ["id1", "id2"])
    assert await cache.get("u", Quality.BEST) == ["id1", "id2"]
    assert await cache.get("u", Quality.AUDIO) is None
    assert await redis.ttl(cache_key("u", Quality.BEST)) > 86000
    await cache.delete("u", Quality.BEST)
    assert await cache.get("u", Quality.BEST) is None


async def test_cache_ignores_empty_and_garbage(redis) -> None:
    cache = FileCache(redis)
    await cache.set("u", "best", [])
    assert await cache.get("u", "best") is None
    await redis.set(cache_key("u", "best"), "not json")
    assert await cache.get("u", "best") is None


async def test_block(redis) -> None:
    cache = FileCache(redis)
    assert not await cache.is_blocked("u")
    await cache.block("u")
    assert await cache.is_blocked("u")


async def test_limits_active(redis) -> None:
    limits = UserLimits(redis, max_active=2, hourly=100)
    assert await limits.try_acquire(1) is None
    assert await limits.try_acquire(1) is None
    assert await limits.try_acquire(1) == "active"
    await limits.release(1)
    assert await limits.try_acquire(1) is None
    # another user is unaffected
    assert await limits.try_acquire(2) is None


async def test_limits_hourly(redis) -> None:
    limits = UserLimits(redis, max_active=100, hourly=3)
    for _ in range(3):
        assert await limits.try_acquire(1) is None
    assert await limits.try_acquire(1) == "hourly"
    # a rejected request does not count against the hour
    assert await redis.get("limits:hourly:1") == b"3"
    assert await redis.ttl("limits:hourly:1") > 0


async def test_release_never_goes_negative(redis) -> None:
    limits = UserLimits(redis)
    await limits.release(9)
    await limits.release(9)
    assert await redis.get("limits:active:9") is None
    assert await limits.try_acquire(9) is None
    assert await redis.get("limits:active:9") == b"1"


async def test_heartbeat_and_stats(redis) -> None:
    assert not await is_alive(redis, Platform.YOUTUBE)
    await beat(redis, Platform.YOUTUBE, 60)
    assert await is_alive(redis, Platform.YOUTUBE)
    await record_result(redis, Platform.YOUTUBE, Result.ok("a", ["x"], 1, cached=True))
    await record_result(redis, Platform.YOUTUBE, Result.error("b", "TOO_LARGE", 1), "group")
    s = await stats(redis, Platform.YOUTUBE)
    assert s == {"ok": 1, "cache_hit": 1, "error": 1, "error:TOO_LARGE": 1}
    assert await stats(redis, Platform.YOUTUBE, "private") == {"ok": 1, "cache_hit": 1}
    assert await stats(redis, Platform.YOUTUBE, "group") == {"error": 1, "error:TOO_LARGE": 1}
    assert await stats(redis, Platform.INSTAGRAM, "group") == {}
