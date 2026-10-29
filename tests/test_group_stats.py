"""G4: /stats splits private vs group traffic and counts groups."""

from __future__ import annotations

import pytest
from arq.connections import ArqRedis

from core.breaker import Breaker
from core.cache import FileCache
from core.contracts import Job, Platform, Result
from core.groups import GroupRegistry, GroupSettings
from core.health import beat, record_result, stats
from core.limits import UserLimits
from core.telegram import FakeSender
from core.worker import run_job
from gateway.handlers import on_stats
from tests.fakes import FakeMessage


@pytest.fixture
async def arq(redis) -> ArqRedis:
    pool = ArqRedis(connection_pool=redis.connection_pool)
    yield pool
    await pool.aclose()


async def test_worker_records_scope(redis, settings) -> None:
    async def extractor(job, dest, settings):
        f = dest / "v.mp4"
        f.write_bytes(b"x")
        return [f]

    for chat_type, url in (("private", "https://x/p"), ("supergroup", "https://x/g")):
        job = Job(
            chat_id=1,
            user_id=2,
            message_id=3,
            url=url,
            platform=Platform.YOUTUBE,
            chat_type=chat_type,
        )
        await run_job(
            job,
            extractor=extractor,
            sender=FakeSender(),
            cache=FileCache(redis),
            limits=UserLimits(redis),
            redis=redis,
            settings=settings,
        )
    assert (await stats(redis, Platform.YOUTUBE))["ok"] == 2
    assert (await stats(redis, Platform.YOUTUBE, "private"))["ok"] == 1
    assert (await stats(redis, Platform.YOUTUBE, "group"))["ok"] == 1


async def test_stats_output_shows_split_and_group_counts(arq, settings) -> None:
    for p in Platform:
        await beat(arq, p, 60)
    await record_result(arq, Platform.YOUTUBE, Result.ok("a", ["x"], 1), "group")
    await record_result(arq, Platform.YOUTUBE, Result.ok("b", ["x"], 1), "private")
    await record_result(arq, Platform.INSTAGRAM, Result.error("c", "PRIVATE", 1), "group")
    groups = GroupRegistry(arq)
    await groups.add(-1, "A", "supergroup", 1)
    await groups.add(-2, "B", "supergroup", 1)
    await groups.save_settings(-2, GroupSettings(auto=True))
    settings.admin_ids = "1"
    m = FakeMessage("/stats", user_id=1)
    await on_stats(m, settings, arq, Breaker(arq), groups)
    text = m.answers[0]
    assert "<b>YouTube</b>" in text and "ok=2 err=0" in text and "in groups: ok=1 err=0" in text
    assert "<b>Instagram</b>" in text and "in groups: ok=0 err=1" in text
    assert "<b>Groups</b>: 2 joined, 1 with auto-download" in text
    assert "<b>Total</b>: ok=2 err=1" in text
