"""Groups the bot is a member of, and their settings, kept in Redis.

`my_chat_member` updates add and remove entries; a group -> supergroup migration moves the
entry and the settings (`group:<id>:settings`) to the new id.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field
from redis.asyncio import Redis

from core.contracts import Platform, Quality

GROUPS_SET = "groups"

# Rungs from lowest to highest; BEST is "no cap".
QUALITY_RANK: dict[Quality, int] = {
    Quality.AUDIO: 0,
    Quality.P360: 1,
    Quality.P480: 2,
    Quality.P720: 3,
    Quality.P1080: 4,
    Quality.BEST: 5,
}
# What /settings cycles through.
MAX_QUALITY_CYCLE: list[Quality] = [
    Quality.BEST,
    Quality.P1080,
    Quality.P720,
    Quality.P480,
    Quality.P360,
    Quality.AUDIO,
]


def cap_quality(wanted: Quality, cap: Quality) -> Quality:
    """The higher of the two limits wins: a 720p request in a 480p group becomes 480p."""
    if cap == Quality.BEST:
        return wanted
    if wanted == Quality.BEST:
        return cap
    return wanted if QUALITY_RANK[wanted] <= QUALITY_RANK[cap] else cap


def within_cap(quality: Quality, cap: Quality) -> bool:
    return cap == Quality.BEST or QUALITY_RANK[quality] <= QUALITY_RANK[cap]


class GroupSettings(BaseModel):
    """Per-group knobs, changed by group admins through /settings."""

    # Download every supported link posted in the group without /dl. On by default: it only
    # takes effect where Telegram shows the bot plain messages (admin, or privacy off), and
    # that is exactly where people expect a downloader bot to just work. /settings turns it off.
    auto: bool = True
    max_quality: Quality = Quality.BEST
    platforms: list[Platform] = Field(default_factory=lambda: list(Platform))
    captions: bool = True  # caption under delivered media (title, stats, description)

    def allows(self, platform: Platform) -> bool:
        return platform in self.platforms

    def to_hash(self) -> dict[str, str]:
        return {
            "auto": "1" if self.auto else "0",
            "max_quality": self.max_quality.value,
            "platforms": ",".join(p.value for p in self.platforms),
            "captions": "1" if self.captions else "0",
        }

    @classmethod
    def from_hash(cls, raw: dict[str, str]) -> GroupSettings:
        data: dict = {}
        if "auto" in raw:
            data["auto"] = raw["auto"] == "1"
        if raw.get("max_quality"):
            try:
                data["max_quality"] = Quality(raw["max_quality"])
            except ValueError:
                pass
        if "captions" in raw:
            data["captions"] = raw["captions"] != "0"
        if "platforms" in raw:
            data["platforms"] = [
                Platform(p)
                for p in raw["platforms"].split(",")
                if p in Platform.__members__.values()
            ]
        return cls(**data)


def group_key(chat_id: int) -> str:
    return f"group:{chat_id}"


def settings_key(chat_id: int) -> str:
    return f"group:{chat_id}:settings"


def _decode(raw: dict) -> dict[str, str]:
    return {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in raw.items()
    }


class GroupRegistry:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def add(self, chat_id: int, title: str, chat_type: str, added_by: int | None) -> None:
        pipe = self._redis.pipeline()
        pipe.hset(
            group_key(chat_id),
            mapping={
                "title": title or "",
                "type": chat_type,
                "added_by": added_by or 0,
                "added_at": int(time.time()),
            },
        )
        pipe.sadd(GROUPS_SET, chat_id)
        await pipe.execute()

    async def _keys(self, chat_id: int) -> list[str]:
        """The group's hash plus every `group:<id>:<suffix>` key. The suffix pattern needs
        the colon: `group:-1*` would also match `group:-100999`."""
        base = group_key(chat_id)
        found = [base] if await self._redis.exists(base) else []
        async for k in self._redis.scan_iter(match=f"{base}:*"):
            found.append(k.decode() if isinstance(k, bytes) else k)
        return found

    async def remove(self, chat_id: int) -> None:
        keys = await self._keys(chat_id)
        pipe = self._redis.pipeline()
        if keys:
            pipe.delete(*keys)
        pipe.srem(GROUPS_SET, chat_id)
        await pipe.execute()

    async def migrate(self, old_id: int, new_id: int) -> bool:
        """Move everything stored under the old id to the new one. False when nothing was."""
        old_prefix = group_key(old_id)
        keys = await self._keys(old_id)
        if not keys:
            return False
        pipe = self._redis.pipeline()
        for name in keys:
            pipe.rename(name, group_key(new_id) + name[len(old_prefix) :])
        pipe.srem(GROUPS_SET, old_id)
        pipe.sadd(GROUPS_SET, new_id)
        pipe.hset(group_key(new_id), "type", "supergroup")
        await pipe.execute()
        return True

    async def set_visibility(self, chat_id: int, can_see: bool) -> None:
        """Whether Telegram delivers plain messages to us here (admin / privacy off)."""
        await self._redis.hset(group_key(chat_id), "can_see", "1" if can_see else "0")

    async def get(self, chat_id: int) -> dict[str, str] | None:
        raw = await self._redis.hgetall(group_key(chat_id))
        return _decode(raw) if raw else None

    async def is_member(self, chat_id: int) -> bool:
        return bool(await self._redis.sismember(GROUPS_SET, chat_id))

    async def count(self) -> int:
        return int(await self._redis.scard(GROUPS_SET))

    # ---- settings ------------------------------------------------------------------

    async def get_settings(self, chat_id: int) -> GroupSettings:
        raw = await self._redis.hgetall(settings_key(chat_id))
        return GroupSettings.from_hash(_decode(raw)) if raw else GroupSettings()

    async def save_settings(self, chat_id: int, settings: GroupSettings) -> None:
        await self._redis.hset(settings_key(chat_id), mapping=settings.to_hash())

    async def auto_off_count(self) -> int:
        """Groups whose admins turned auto-download off (it is on by default)."""
        n = 0
        async for k in self._redis.scan_iter(match="group:*:settings"):
            if await self._redis.hget(k, "auto") in (b"0", "0"):
                n += 1
        return n
