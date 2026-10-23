"""Circuit breaker per platform, kept in Redis so the gateway and workers share one view.

`threshold` transient failures inside `window_s` open the breaker for `open_for_s`. While it is
open the gateway refuses new jobs for that platform and tells the user which platforms still
work. A success closes the failure window early.
"""

from __future__ import annotations

import time
import uuid

from redis.asyncio import Redis

from core.contracts import Platform


def fail_key(platform: Platform) -> str:
    return f"breaker:fail:{platform}"


def open_key(platform: Platform) -> str:
    return f"breaker:open:{platform}"


class Breaker:
    def __init__(
        self, redis: Redis, threshold: int = 5, window_s: int = 600, open_for_s: int = 900
    ) -> None:
        self._redis = redis
        self.threshold = threshold
        self.window_s = window_s
        self.open_for_s = open_for_s

    async def record_failure(self, platform: Platform) -> bool:
        """Count one transient failure. Returns True when this one opened the breaker."""
        now = time.time()
        key = fail_key(platform)
        pipe = self._redis.pipeline()
        pipe.zadd(key, {uuid.uuid4().hex: now})
        pipe.zremrangebyscore(key, 0, now - self.window_s)
        pipe.zcard(key)
        pipe.expire(key, self.window_s)
        _, _, count, _ = await pipe.execute()
        if count < self.threshold:
            return False
        already = await self._redis.set(open_key(platform), f"{now}", ex=self.open_for_s, nx=True)
        await self._redis.delete(key)
        return bool(already)

    async def record_success(self, platform: Platform) -> None:
        await self._redis.delete(fail_key(platform))

    async def is_open(self, platform: Platform) -> bool:
        return bool(await self._redis.exists(open_key(platform)))

    async def opened_at(self, platform: Platform) -> float | None:
        raw = await self._redis.get(open_key(platform))
        return float(raw) if raw else None

    async def failures(self, platform: Platform) -> int:
        return int(await self._redis.zcard(fail_key(platform)))

    async def reset(self, platform: Platform) -> None:
        await self._redis.delete(open_key(platform), fail_key(platform))
