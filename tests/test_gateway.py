"""Gateway handler tests with a fake Message and an arq pool backed by fakeredis."""

from __future__ import annotations

import asyncio
import json

import pytest
from arq.connections import ArqRedis
from arq.jobs import deserialize_job

from core.breaker import Breaker
from core.cache import FileCache
from core.contracts import QUEUE_NAMES, FormatOption, Job, Platform, Quality
from core.health import beat
from core.limits import UserLimits
from gateway.handlers import (
    WORKING,
    on_quality_chosen,
    on_start,
    on_stats,
    on_text,
    pending_key,
    probe_key,
)
from tests.fakes import FakeMessage, FakeQuery

YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
MENU = [
    FormatOption(quality=Quality.P720, size_bytes=20_000_000, height=720),
    FormatOption(quality=Quality.P360, size_bytes=5_000_000, height=360),
    FormatOption(quality=Quality.AUDIO, size_bytes=1_000_000),
]


@pytest.fixture
async def arq(redis) -> ArqRedis:
    # fakeredis' FakeRedis speaks the same protocol; wrap it with the arq client class.
    pool = ArqRedis(connection_pool=redis.connection_pool)
    yield pool
    await pool.aclose()


@pytest.fixture
def deps(arq, settings):
    settings.quality_choice_timeout = 1
    return {
        "settings": settings,
        "arq": arq,
        "limits": UserLimits(arq, settings.max_active_jobs, settings.hourly_limit),
        "cache": FileCache(arq),
        "breaker": Breaker(arq),
    }


async def _all_up(arq: ArqRedis) -> None:
    for p in Platform:
        await beat(arq, p, 60)


async def _set_menu(arq: ArqRedis, url: str, menu: list[FormatOption]) -> None:
    """Pre-fill the probe cache so tests don't need a worker answering probes."""
    await arq.set(probe_key(url), json.dumps([f.model_dump(mode="json") for f in menu]))


async def _queued_jobs(arq: ArqRedis, platform: Platform) -> list[Job]:
    ids = await arq.zrange(QUEUE_NAMES[platform], 0, -1)
    jobs = []
    for jid in ids:
        raw = await arq.get(f"arq:job:{jid.decode()}")
        jobs.append(Job.model_validate(deserialize_job(raw).args[0]))
    return jobs


async def _queued_job(arq: ArqRedis, platform: Platform) -> Job:
    jobs = await _queued_jobs(arq, platform)
    assert len(jobs) == 1
    return jobs[0]


async def test_start(settings) -> None:
    m = FakeMessage("/start")
    await on_start(m, settings)
    assert "YouTube" in m.answers[0]


async def test_youtube_link_without_menu_is_enqueued(arq, deps) -> None:
    await _all_up(arq)
    await _set_menu(arq, YT, [])  # probe says: nothing to choose from
    m = FakeMessage("check this https://youtu.be/dQw4w9WgXcQ?si=xyz")
    await on_text(m, **deps)
    assert m.replies == [WORKING]
    job = await _queued_job(arq, Platform.YOUTUBE)
    assert job.url == YT
    assert job.chat_id == 99 and job.user_id == 7 and job.message_id == 555
    assert job.quality is Quality.BEST
    assert await arq.get("limits:active:7") == b"1"


async def test_youtube_menu_then_choice(arq, deps) -> None:
    await _all_up(arq)
    await _set_menu(arq, YT, MENU)
    m = FakeMessage("https://youtu.be/dQw4w9WgXcQ")
    await on_text(m, **deps)
    assert m.replies[0] == WORKING and m.replies[1].startswith("Pick a quality")
    kb = m.markups[0]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert labels == ["720p · 19 MB", "360p · 4.8 MB", "Audio only · 1.0 MB"]
    assert await _queued_jobs(arq, Platform.YOUTUBE) == []  # nothing queued yet
    job_id = kb.inline_keyboard[0][0].callback_data.split(":")[1]
    assert await arq.exists(pending_key(job_id))

    q = FakeQuery(f"q:{job_id}:360p")
    await on_quality_chosen(q, deps["settings"], arq, deps["limits"])
    job = await _queued_job(arq, Platform.YOUTUBE)
    assert job.quality is Quality.P360 and job.job_id == job_id
    assert not await arq.exists(pending_key(job_id))

    # a second click on the same menu is rejected
    q2 = FakeQuery(f"q:{job_id}:720p")
    await on_quality_chosen(q2, deps["settings"], arq, deps["limits"])
    assert "expired" in q2.answers[0][0][0]
    assert len(await _queued_jobs(arq, Platform.YOUTUBE)) == 1


async def test_youtube_menu_defaults_after_timeout(arq, deps) -> None:
    await _all_up(arq)
    await _set_menu(arq, YT, MENU)
    m = FakeMessage("https://youtu.be/dQw4w9WgXcQ")
    await on_text(m, **deps)
    assert await _queued_jobs(arq, Platform.YOUTUBE) == []
    await asyncio.sleep(1.3)
    job = await _queued_job(arq, Platform.YOUTUBE)
    assert job.quality is Quality.P720  # best rung under the cap
    assert m.replies[-1] == WORKING


async def test_other_user_cannot_choose(arq, deps) -> None:
    await _all_up(arq)
    await _set_menu(arq, YT, MENU)
    m = FakeMessage("https://youtu.be/dQw4w9WgXcQ")
    await on_text(m, **deps)
    job_id = m.markups[0].inline_keyboard[0][0].callback_data.split(":")[1]
    q = FakeQuery(f"q:{job_id}:720p", user_id=8)
    await on_quality_chosen(q, deps["settings"], arq, deps["limits"])
    assert "Only the person" in q.answers[0][0][0]
    assert await arq.exists(pending_key(job_id))  # still pending for the right user


async def test_audio_keyword_skips_menu(arq, deps) -> None:
    await _all_up(arq)
    await _set_menu(arq, YT, MENU)
    m = FakeMessage("https://youtu.be/dQw4w9WgXcQ audio")
    await on_text(m, **deps)
    assert (await _queued_job(arq, Platform.YOUTUBE)).quality is Quality.AUDIO


async def test_cached_default_skips_menu(arq, deps) -> None:
    await _all_up(arq)
    await _set_menu(arq, YT, MENU)
    await deps["cache"].set(YT, Quality.BEST, ["fid"])
    m = FakeMessage("https://youtu.be/dQw4w9WgXcQ")
    await on_text(m, **deps)
    assert (await _queued_job(arq, Platform.YOUTUBE)).quality is Quality.BEST
    assert m.markups == []


async def test_instagram_has_no_menu(arq, deps) -> None:
    await _all_up(arq)
    m = FakeMessage("https://www.instagram.com/p/C1a2B3c4D5e/")
    await on_text(m, **deps)
    assert (await _queued_job(arq, Platform.INSTAGRAM)).platform is Platform.INSTAGRAM
    assert m.markups == []


async def test_unsupported_link(arq, deps) -> None:
    m = FakeMessage("https://tiktok.com/@a/video/1")
    await on_text(m, **deps)
    assert "can't handle" in m.answers[0]
    assert await arq.zcard(QUEUE_NAMES[Platform.YOUTUBE]) == 0


async def test_no_link(arq, deps) -> None:
    m = FakeMessage("hello")
    await on_text(m, **deps)
    assert "Send me a link" in m.answers[0]


async def test_dead_worker_refuses_but_names_others(arq, deps) -> None:
    await beat(arq, Platform.INSTAGRAM, 60)
    await beat(arq, Platform.PINTEREST, 60)
    m = FakeMessage("https://youtu.be/dQw4w9WgXcQ")
    await on_text(m, **deps)
    assert m.answers[0] == "YouTube is temporarily unavailable. Instagram and Pinterest still work."
    assert m.replies == []

    m2 = FakeMessage("https://www.instagram.com/p/C1a2B3c4D5e/")
    await on_text(m2, **deps)
    assert (await _queued_job(arq, Platform.INSTAGRAM)).platform is Platform.INSTAGRAM


async def test_open_breaker_refuses(arq, deps) -> None:
    await _all_up(arq)
    b = Breaker(arq, threshold=1)
    await b.record_failure(Platform.PINTEREST)
    await b.record_failure(Platform.INSTAGRAM)
    m = FakeMessage("https://pin.it/abc")
    await on_text(m, **deps)
    assert m.answers[0] == "Pinterest is temporarily unavailable. YouTube still works."


async def test_active_limit(arq, deps) -> None:
    await _all_up(arq)
    for _ in range(deps["settings"].max_active_jobs):
        await on_text(FakeMessage("https://pin.it/abc"), **deps)
    m = FakeMessage("https://pin.it/abc")
    await on_text(m, **deps)
    assert "downloads running" in m.answers[0]


async def test_blocked(arq, deps) -> None:
    await _all_up(arq)
    await deps["cache"].block(YT)
    m = FakeMessage("https://youtu.be/dQw4w9WgXcQ")
    await on_text(m, **deps)
    assert "blocked" in m.answers[0]


async def test_stats_admin_only(arq, deps) -> None:
    await _all_up(arq)
    m = FakeMessage("/stats", user_id=1)
    await on_stats(m, deps["settings"], arq, deps["breaker"])
    assert m.answers == []
    deps["settings"].admin_ids = "1"
    await on_stats(m, deps["settings"], arq, deps["breaker"])
    assert "YouTube" in m.answers[0] and "🟢" in m.answers[0]
