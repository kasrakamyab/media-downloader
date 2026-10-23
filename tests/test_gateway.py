"""Gateway handler tests with a fake Message and an arq pool backed by fakeredis."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from arq.connections import ArqRedis
from arq.jobs import deserialize_job

from core.cache import FileCache
from core.contracts import QUEUE_NAMES, Job, Platform, Quality
from core.health import beat
from core.limits import UserLimits
from gateway.handlers import on_start, on_text


class FakeMessage:
    def __init__(self, text: str, user_id: int = 7, chat_id: int = 99) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=chat_id)
        self.answers: list[str] = []
        self.replies: list[str] = []

    async def answer(self, text: str, **_) -> None:
        self.answers.append(text)

    async def reply(self, text: str, **_) -> SimpleNamespace:
        self.replies.append(text)
        return SimpleNamespace(message_id=555, edit_text=self._edit)

    async def _edit(self, text: str) -> None:
        self.replies.append(text)


@pytest.fixture
async def arq(redis) -> ArqRedis:
    # fakeredis' FakeRedis speaks the same protocol; wrap it with the arq client class.
    pool = ArqRedis(connection_pool=redis.connection_pool)
    yield pool
    await pool.aclose()


@pytest.fixture
def deps(arq, settings):
    return {
        "settings": settings,
        "arq": arq,
        "limits": UserLimits(arq, settings.max_active_jobs, settings.hourly_limit),
        "cache": FileCache(arq),
    }


async def _queued_job(arq: ArqRedis, platform: Platform) -> Job:
    queue = QUEUE_NAMES[platform]
    ids = await arq.zrange(queue, 0, -1)
    assert len(ids) == 1
    raw = await arq.get(f"arq:job:{ids[0].decode()}")
    jd = deserialize_job(raw)
    return Job.model_validate(jd.args[0])


async def test_start(settings) -> None:
    m = FakeMessage("/start")
    await on_start(m, settings)
    assert "YouTube" in m.answers[0]


async def test_youtube_link_is_enqueued(arq, deps) -> None:
    await beat(arq, Platform.YOUTUBE, 60)
    m = FakeMessage("check this https://youtu.be/dQw4w9WgXcQ?si=xyz")
    await on_text(m, **deps)
    assert m.replies == ["⏳ Working on it…"]
    job = await _queued_job(arq, Platform.YOUTUBE)
    assert job.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert job.chat_id == 99 and job.user_id == 7 and job.message_id == 555
    assert job.quality is Quality.BEST
    assert await arq.get("limits:active:7") == b"1"


async def test_audio_keyword(arq, deps) -> None:
    await beat(arq, Platform.YOUTUBE, 60)
    m = FakeMessage("https://youtu.be/dQw4w9WgXcQ audio")
    await on_text(m, **deps)
    assert (await _queued_job(arq, Platform.YOUTUBE)).quality is Quality.AUDIO


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
    m = FakeMessage("https://youtu.be/dQw4w9WgXcQ")
    await on_text(m, **deps)
    assert "YouTube is temporarily unavailable" in m.answers[0]
    assert "Instagram and Pinterest" in m.answers[0]
    assert m.replies == []

    m2 = FakeMessage("https://www.instagram.com/p/C1a2B3c4D5e/")
    await on_text(m2, **deps)
    assert (await _queued_job(arq, Platform.INSTAGRAM)).platform is Platform.INSTAGRAM


async def test_active_limit(arq, deps) -> None:
    await beat(arq, Platform.PINTEREST, 60)
    for _ in range(deps["settings"].max_active_jobs):
        await on_text(FakeMessage("https://pin.it/abc"), **deps)
    m = FakeMessage("https://pin.it/abc")
    await on_text(m, **deps)
    assert "downloads running" in m.answers[0]


async def test_blocked(arq, deps) -> None:
    await beat(arq, Platform.YOUTUBE, 60)
    await deps["cache"].block("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    m = FakeMessage("https://youtu.be/dQw4w9WgXcQ")
    await on_text(m, **deps)
    assert "blocked" in m.answers[0]
