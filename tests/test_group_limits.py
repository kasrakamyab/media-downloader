"""G3: per-group limits and fair sharing of worker slots."""

from __future__ import annotations

import pytest
from arq.connections import ArqRedis
from arq.jobs import deserialize_job

from core.breaker import Breaker
from core.cache import FileCache
from core.contracts import QUEUE_NAMES, Job, Platform
from core.groups import GroupRegistry, GroupSettings
from core.health import beat
from core.limits import UserLimits
from core.telegram import FakeSender
from core.worker import run_job
from gateway.handlers import on_text, probe_key
from tests.fakes import FakeMessage

YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
GROUP = -100123


async def test_group_active_cap_is_shared_by_members(redis) -> None:
    limits = UserLimits(redis, max_active=5, hourly=100, chat_max_active=2, chat_hourly=100)
    assert await limits.try_acquire(1, GROUP) is None
    assert await limits.try_acquire(2, GROUP) is None
    assert await limits.try_acquire(3, GROUP) == "group_active"
    # nothing held after a refusal
    assert await redis.get("limits:active:3") is None
    assert int(await redis.get("limits:hourly:3") or 0) == 0
    assert await redis.get("limits:chat_hourly:-100123") == b"2"
    # private chats are unaffected by a busy group
    assert await limits.try_acquire(3) is None
    # release frees the group slot
    await limits.release(1, GROUP)
    assert await limits.try_acquire(3, GROUP) is None


async def test_group_hourly_budget(redis) -> None:
    limits = UserLimits(redis, max_active=100, hourly=100, chat_max_active=100, chat_hourly=3)
    for user in (1, 2, 3):
        assert await limits.try_acquire(user, GROUP) is None
        await limits.release(user, GROUP)
    assert await limits.try_acquire(4, GROUP) == "group_hourly"
    assert await redis.get("limits:active:4") is None
    assert 0 < await redis.ttl("limits:chat_hourly:-100123") <= 3600
    assert await limits.try_acquire(4, GROUP + 1) is None  # another group has its own budget


async def test_user_limits_still_apply_in_groups(redis) -> None:
    limits = UserLimits(redis, max_active=1, hourly=100, chat_max_active=10, chat_hourly=100)
    assert await limits.try_acquire(1, GROUP) is None
    assert await limits.try_acquire(1, GROUP) == "active"
    assert await redis.get("limits:chat_active:-100123") == b"1"  # the refusal took nothing


async def test_release_without_chat_leaves_chat_counters(redis) -> None:
    limits = UserLimits(redis)
    await limits.try_acquire(1, GROUP)
    await limits.release(1)  # a private-chat style release
    assert await redis.get("limits:chat_active:-100123") == b"1"
    await limits.release(1, GROUP)
    assert await redis.get("limits:chat_active:-100123") is None


@pytest.fixture
async def arq(redis) -> ArqRedis:
    pool = ArqRedis(connection_pool=redis.connection_pool)
    yield pool
    await pool.aclose()


@pytest.fixture
async def deps(arq, settings):
    for p in Platform:
        await beat(arq, p, 60)
    await arq.set(probe_key(YT), "[]")
    settings.max_active_per_chat = 1
    return {
        "settings": settings,
        "arq": arq,
        "limits": UserLimits(
            arq, 5, 100, chat_max_active=settings.max_active_per_chat, chat_hourly=100
        ),
        "cache": FileCache(arq),
        "breaker": Breaker(arq),
        "bot_username": "Bot",
        "groups": GroupRegistry(arq),
    }


async def _queued(arq: ArqRedis, platform: Platform) -> list[Job]:
    ids = await arq.zrange(QUEUE_NAMES[platform], 0, -1)
    out = []
    for jid in ids:
        raw = await arq.get(f"arq:job:{jid.decode()}")
        out.append(Job.model_validate(deserialize_job(raw).args[0]))
    return out


async def test_gateway_tells_group_when_its_cap_is_hit(arq, deps) -> None:
    first = FakeMessage(f"/dl {YT}", user_id=1, chat_id=GROUP, chat_type="supergroup")
    await on_text(first, **deps)
    second = FakeMessage(f"/dl {YT}", user_id=2, chat_id=GROUP, chat_type="supergroup")
    await on_text(second, **deps)
    assert "This group already has 1 downloads running" in second.replies[0]
    assert len(await _queued(arq, Platform.YOUTUBE)) == 1
    # a private chat is not blocked by the busy group
    private = FakeMessage(YT, user_id=3, chat_id=3)
    await on_text(private, **deps)
    assert len(await _queued(arq, Platform.YOUTUBE)) == 2


async def test_auto_mode_cap_is_silent(arq, deps) -> None:
    await deps["groups"].save_settings(GROUP, GroupSettings(auto=True))
    for user in (1, 2):
        m = FakeMessage(f"see {YT}", user_id=user, chat_id=GROUP, chat_type="supergroup")
        await on_text(m, **deps)
        assert m.replies == [] and m.answers == []
    assert len(await _queued(arq, Platform.YOUTUBE)) == 1


async def test_worker_releases_group_slot(redis, settings) -> None:
    limits = UserLimits(redis, chat_max_active=1)
    assert await limits.try_acquire(2, GROUP) is None
    job = Job(
        chat_id=GROUP,
        user_id=2,
        message_id=3,
        url="https://x/l",
        platform=Platform.YOUTUBE,
        chat_type="supergroup",
    )

    async def extractor(job, dest, settings):
        f = dest / "v.mp4"
        f.write_bytes(b"x")
        return [f]

    await run_job(
        job,
        extractor=extractor,
        sender=FakeSender(),
        cache=FileCache(redis),
        limits=limits,
        redis=redis,
        settings=settings,
    )
    assert await redis.get("limits:chat_active:-100123") is None
    assert await redis.get("limits:active:2") is None
    assert await limits.try_acquire(9, GROUP) is None
