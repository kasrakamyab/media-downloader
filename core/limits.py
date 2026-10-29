"""Per-user and per-chat limits kept in Redis.

Users: max active jobs and requests per hour, everywhere. Groups additionally get a cap on
jobs in flight and an hourly budget for the whole chat, so one busy group can neither hog
the workers nor starve private chats. The gateway acquires before enqueuing; the worker
releases in a `finally`. Keys carry a TTL so a lost worker can never lock anyone out for good.
"""

from __future__ import annotations

from redis.asyncio import Redis


def active_key(user_id: int) -> str:
    return f"limits:active:{user_id}"


def hourly_key(user_id: int) -> str:
    return f"limits:hourly:{user_id}"


def chat_active_key(chat_id: int) -> str:
    return f"limits:chat_active:{chat_id}"


def chat_hourly_key(chat_id: int) -> str:
    return f"limits:chat_hourly:{chat_id}"


class UserLimits:
    def __init__(
        self,
        redis: Redis,
        max_active: int = 2,
        hourly: int = 20,
        active_ttl: int = 900,
        *,
        chat_max_active: int = 2,
        chat_hourly: int = 60,
    ) -> None:
        self._redis = redis
        self.max_active = max_active
        self.hourly = hourly
        self.chat_max_active = chat_max_active
        self.chat_hourly = chat_hourly
        self._active_ttl = active_ttl

    async def _take(self, key: str, cap: int, ttl: int) -> bool:
        count = await self._redis.incr(key)
        if count == 1 or ttl == self._active_ttl:
            await self._redis.expire(key, ttl)
        if count > cap:
            await self._redis.decr(key)
            return False
        return True

    async def _give_back(self, key: str) -> None:
        if await self._redis.decr(key) <= 0:
            await self._redis.delete(key)

    async def try_acquire(self, user_id: int, chat_id: int | None = None) -> str | None:
        """Reserve a slot for this user (and, in a group, for the chat).

        Returns None on success, else which limit was hit: "hourly", "active",
        "group_hourly" or "group_active". Nothing is held after a refusal.
        """
        if not await self._take(hourly_key(user_id), self.hourly, 3600):
            return "hourly"
        if not await self._take(active_key(user_id), self.max_active, self._active_ttl):
            await self._redis.decr(hourly_key(user_id))
            return "active"
        if chat_id is None:
            return None
        if not await self._take(chat_hourly_key(chat_id), self.chat_hourly, 3600):
            await self._give_back(active_key(user_id))
            await self._redis.decr(hourly_key(user_id))
            return "group_hourly"
        if not await self._take(chat_active_key(chat_id), self.chat_max_active, self._active_ttl):
            await self._redis.decr(chat_hourly_key(chat_id))
            await self._give_back(active_key(user_id))
            await self._redis.decr(hourly_key(user_id))
            return "group_active"
        return None

    async def release(self, user_id: int, chat_id: int | None = None) -> None:
        await self._give_back(active_key(user_id))
        if chat_id is not None:
            await self._give_back(chat_active_key(chat_id))
