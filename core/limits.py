"""Per-user limits kept in Redis: active jobs and requests per hour.

The gateway acquires a slot before enqueuing; the worker releases it in a `finally`.
Keys carry a TTL so a lost worker can never lock a user out for good.
"""

from __future__ import annotations

from redis.asyncio import Redis


def active_key(user_id: int) -> str:
    return f"limits:active:{user_id}"


def hourly_key(user_id: int) -> str:
    return f"limits:hourly:{user_id}"


class UserLimits:
    def __init__(
        self, redis: Redis, max_active: int = 2, hourly: int = 20, active_ttl: int = 900
    ) -> None:
        self._redis = redis
        self.max_active = max_active
        self.hourly = hourly
        self._active_ttl = active_ttl

    async def try_acquire(self, user_id: int) -> str | None:
        """Reserve a slot. Returns None on success, or a reason string when a limit is hit."""
        hk = hourly_key(user_id)
        count = await self._redis.incr(hk)
        if count == 1:
            await self._redis.expire(hk, 3600)
        if count > self.hourly:
            await self._redis.decr(hk)
            return "hourly"

        ak = active_key(user_id)
        active = await self._redis.incr(ak)
        await self._redis.expire(ak, self._active_ttl)
        if active > self.max_active:
            await self._redis.decr(ak)
            await self._redis.decr(hk)
            return "active"
        return None

    async def release(self, user_id: int) -> None:
        ak = active_key(user_id)
        if await self._redis.decr(ak) <= 0:
            await self._redis.delete(ak)
