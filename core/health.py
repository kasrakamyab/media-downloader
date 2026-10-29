"""Worker heartbeats and job/result bookkeeping shared by gateway and workers."""

from __future__ import annotations

from redis.asyncio import Redis

from core.contracts import Platform, Result


def heartbeat_key(platform: Platform) -> str:
    return f"heartbeat:{platform}"


async def beat(redis: Redis, platform: Platform, ttl: int) -> None:
    await redis.set(heartbeat_key(platform), "1", ex=ttl)


async def is_alive(redis: Redis, platform: Platform) -> bool:
    return bool(await redis.exists(heartbeat_key(platform)))


def stats_key(platform: Platform, scope: str | None = None) -> str:
    """`stats:<platform>` holds the totals; `stats:<platform>:group` / `:private` the split."""
    return f"stats:{platform}" if scope is None else f"stats:{platform}:{scope}"


async def record_result(
    redis: Redis, platform: Platform, result: Result, scope: str = "private"
) -> None:
    """Keep the last result per job (1 h) and roll up counters for /stats, both in total
    and per scope ("private" or "group")."""
    pipe = redis.pipeline()
    pipe.set(f"result:{result.job_id}", result.model_dump_json(), ex=3600)
    for key in (stats_key(platform), stats_key(platform, scope)):
        pipe.hincrby(key, result.status, 1)
        if result.cached:
            pipe.hincrby(key, "cache_hit", 1)
        if result.error_code:
            pipe.hincrby(key, f"error:{result.error_code}", 1)
    await pipe.execute()


async def stats(redis: Redis, platform: Platform, scope: str | None = None) -> dict[str, int]:
    raw = await redis.hgetall(stats_key(platform, scope))
    return {(k.decode() if isinstance(k, bytes) else k): int(v) for k, v in raw.items()}
