"""Redis cache: normalised URL + quality -> Telegram file_ids (+ the source metadata).

A hit means the worker re-sends by file_id: no download, no CPU, no disk. The metadata is
kept alongside so the re-send can carry the same caption, built in whatever caption mode
the receiving chat wants.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from redis.asyncio import Redis

from core.captions import MediaMeta
from core.contracts import Quality

_PREFIX = "cache"


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:32]


def cache_key(url: str, quality: Quality | str) -> str:
    return f"{_PREFIX}:{url_hash(url)}:{quality}"


@dataclass(slots=True)
class CachedMedia:
    file_ids: list[str]
    meta: MediaMeta | None = None
    note: str | None = None  # e.g. the Instagram preview warning
    extra: dict = field(default_factory=dict)


class FileCache:
    def __init__(self, redis: Redis, ttl_days: int = 30) -> None:
        self._redis = redis
        self._ttl = ttl_days * 86400

    async def get_entry(self, url: str, quality: Quality | str) -> CachedMedia | None:
        raw = await self._redis.get(cache_key(url, quality))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if isinstance(data, list):  # entries written before metadata was stored
            return CachedMedia(data) if data else None
        if not isinstance(data, dict) or not data.get("ids"):
            return None
        meta = None
        if isinstance(data.get("meta"), dict):
            try:
                meta = MediaMeta.model_validate(data["meta"])
            except ValueError:
                meta = None
        return CachedMedia(list(data["ids"]), meta, data.get("note") or None)

    async def get(self, url: str, quality: Quality | str) -> list[str] | None:
        entry = await self.get_entry(url, quality)
        return entry.file_ids if entry else None

    async def set(
        self,
        url: str,
        quality: Quality | str,
        file_ids: list[str],
        meta: MediaMeta | None = None,
        note: str | None = None,
    ) -> None:
        if not file_ids:
            return
        payload: dict = {"ids": list(file_ids)}
        if meta is not None:
            payload["meta"] = meta.model_dump(exclude_none=True)
        if note:
            payload["note"] = note
        await self._redis.set(cache_key(url, quality), json.dumps(payload), ex=self._ttl)

    async def delete(self, url: str, quality: Quality | str) -> None:
        await self._redis.delete(cache_key(url, quality))

    async def is_blocked(self, url: str) -> bool:
        """Takedown support: block a URL hash so it is never served again."""
        return bool(await self._redis.exists(f"blocked:{url_hash(url)}"))

    async def block(self, url: str) -> None:
        await self._redis.set(f"blocked:{url_hash(url)}", "1")
