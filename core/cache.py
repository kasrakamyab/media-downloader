"""Redis cache: normalised URL + quality -> Telegram file_ids.

A hit means the worker re-sends by file_id: no download, no CPU, no disk.
"""

from __future__ import annotations

import hashlib
import json

from redis.asyncio import Redis

from core.contracts import Quality

_PREFIX = "cache"


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:32]


def cache_key(url: str, quality: Quality | str) -> str:
    return f"{_PREFIX}:{url_hash(url)}:{quality}"


class FileCache:
    def __init__(self, redis: Redis, ttl_days: int = 30) -> None:
        self._redis = redis
        self._ttl = ttl_days * 86400

    async def get(self, url: str, quality: Quality | str) -> list[str] | None:
        raw = await self._redis.get(cache_key(url, quality))
        if not raw:
            return None
        try:
            ids = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return ids if isinstance(ids, list) and ids else None

    async def set(self, url: str, quality: Quality | str, file_ids: list[str]) -> None:
        if not file_ids:
            return
        await self._redis.set(cache_key(url, quality), json.dumps(file_ids), ex=self._ttl)

    async def delete(self, url: str, quality: Quality | str) -> None:
        await self._redis.delete(cache_key(url, quality))

    async def is_blocked(self, url: str) -> bool:
        """Takedown support: block a URL hash so it is never served again."""
        return bool(await self._redis.exists(f"blocked:{url_hash(url)}"))

    async def block(self, url: str) -> None:
        await self._redis.set(f"blocked:{url_hash(url)}", "1")
