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


async def record_result(redis: Redis, platform: Platform, result: Result) -> None:
    """Keep the last result per job (1 h) and roll up counters for /stats."""
    pipe = redis.pipeline()
    pipe.set(f"result:{result.job_id}", result.model_dump_json(), ex=3600)
    pipe.hincrby(f"stats:{platform}", result.status, 1)
    if result.cached:
        pipe.hincrby(f"stats:{platform}", "cache_hit", 1)
    if result.error_code:
        pipe.hincrby(f"stats:{platform}", f"error:{result.error_code}", 1)
    await pipe.execute()


async def stats(redis: Redis, platform: Platform) -> dict[str, int]:
    raw = await redis.hgetall(f"stats:{platform}")
    return {(k.decode() if isinstance(k, bytes) else k): int(v) for k, v in raw.items()}
