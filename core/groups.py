"""Groups the bot is a member of, kept in Redis.

`my_chat_member` updates add and remove entries; a group -> supergroup migration moves the
entry (and any per-group settings, which later steps add under `group:<id>:*`) to the new id.
"""

from __future__ import annotations

import time

from redis.asyncio import Redis

GROUPS_SET = "groups"


def group_key(chat_id: int) -> str:
    return f"group:{chat_id}"


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

    async def get(self, chat_id: int) -> dict[str, str] | None:
        raw = await self._redis.hgetall(group_key(chat_id))
        if not raw:
            return None
        return {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in raw.items()
        }

    async def is_member(self, chat_id: int) -> bool:
        return bool(await self._redis.sismember(GROUPS_SET, chat_id))

    async def count(self) -> int:
        return int(await self._redis.scard(GROUPS_SET))
