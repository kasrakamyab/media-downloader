"""Progress as reactions (👀 -> 👍 / 👎) instead of a placeholder message, with fallback."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from arq.connections import ArqRedis
from arq.jobs import deserialize_job

from core.breaker import Breaker
from core.cache import FileCache
from core.contracts import QUEUE_NAMES, ErrorCode, FormatOption, Job, Platform, Quality
from core.groups import GroupRegistry, GroupSettings
from core.health import beat
from core.limits import UserLimits
from core.telegram import (
    REACT_DONE,
    REACT_FAILED,
    REACT_WORKING,
    FakeSender,
    TelegramSender,
)
from core.worker import ExtractError, run_job
from gateway.handlers import WORKING, on_quality_chosen, on_text, pending_key, probe_key
from tests.fakes import FakeBot, FakeMessage, FakeQuery

YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
GROUP = -100123
MENU = [
    FormatOption(quality=Quality.P720, size_bytes=20_000_000),
    FormatOption(quality=Quality.P360, size_bytes=5_000_000),
]


class ReactingBot(FakeBot):
    def __init__(self, works: bool = True) -> None:
        super().__init__()
        self.works = works
        self.reactions: list[tuple[int, int, list[str]]] = []

    async def set_message_reaction(self, chat_id: int, message_id: int, reaction=None, **_):
        if not self.works:
            raise TelegramBadRequest(method=SimpleNamespace(), message="REACTION_INVALID")
        self.reactions.append((chat_id, message_id, [r.emoji for r in reaction or []]))


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
    settings.quality_choice_timeout = 1
    return {
        "settings": settings,
        "arq": arq,
        "limits": UserLimits(arq, 5, 100),
        "cache": FileCache(arq),
        "breaker": Breaker(arq),
        "bot_username": "Bot",
        "groups": GroupRegistry(arq),
    }


async def _queued(arq: ArqRedis) -> list[Job]:
    ids = await arq.zrange(QUEUE_NAMES[Platform.YOUTUBE], 0, -1)
    out = []
    for jid in ids:
        raw = await arq.get(f"arq:job:{jid.decode()}")
        out.append(Job.model_validate(deserialize_job(raw).args[0]))
    return out


# ---- gateway ----------------------------------------------------------------------------


async def test_reaction_replaces_placeholder(arq, deps) -> None:
    bot = ReactingBot()
    m = FakeMessage(YT)
    await on_text(m, bot=bot, **deps)
    assert bot.reactions == [(99, m.message_id, [REACT_WORKING])]
    assert m.replies == [] and m.answers == []  # no "Working on it…"
    (job,) = await _queued(arq)
    assert job.react_message_id == m.message_id and job.message_id == 0 and not job.quiet


async def test_falls_back_to_placeholder_when_reactions_fail(arq, deps) -> None:
    m = FakeMessage(YT)
    await on_text(m, bot=ReactingBot(works=False), **deps)
    assert m.replies == [WORKING]
    (job,) = await _queued(arq)
    assert job.react_message_id is None and job.message_id == 555


async def test_reactions_can_be_turned_off(arq, deps) -> None:
    deps["settings"].use_reactions = False
    bot = ReactingBot()
    m = FakeMessage(YT)
    await on_text(m, bot=bot, **deps)
    assert bot.reactions == [] and m.replies == [WORKING]


async def test_group_reply_reacts_on_the_message_with_the_link(arq, deps) -> None:
    bot = ReactingBot()
    original = FakeMessage(YT, user_id=3, chat_id=GROUP, chat_type="supergroup")
    m = FakeMessage("/dl", chat_id=GROUP, chat_type="supergroup", reply_to=original)
    await on_text(m, bot=bot, **deps)
    assert bot.reactions == [(GROUP, original.message_id, [REACT_WORKING])]
    (job,) = await _queued(arq)
    assert job.react_message_id == original.message_id == job.reply_to_message_id


async def test_auto_mode_reacts_but_posts_nothing(arq, deps) -> None:
    await deps["groups"].save_settings(GROUP, GroupSettings(auto=True))
    bot = ReactingBot()
    m = FakeMessage(f"see {YT}", chat_id=GROUP, chat_type="supergroup")
    await on_text(m, bot=bot, **deps)
    assert bot.reactions and m.replies == []
    (job,) = await _queued(arq)
    assert job.quiet and job.react_message_id == m.message_id


async def test_menu_is_a_reply_and_is_deleted_after_the_choice(arq, deps) -> None:
    import json

    await arq.set(probe_key(YT), json.dumps([f.model_dump(mode="json") for f in MENU]))
    bot = ReactingBot()
    m = FakeMessage(YT)
    await on_text(m, bot=bot, **deps)
    assert m.replies[0].startswith("Pick a quality") and m.markups  # menu, no placeholder
    assert await _queued(arq) == []
    job_id = m.markups[0].inline_keyboard[0][0].callback_data.split(":")[1]
    pending = await arq.get(pending_key(job_id))
    assert b'"react_message_id":' in pending and b'"message_id":555' in pending

    q = FakeQuery(f"q:{job_id}:360p")
    q.message = await m.reply("menu")  # stands in for the menu message
    m.replies.pop()
    await on_quality_chosen(q, deps["settings"], arq, deps["limits"])
    (job,) = await _queued(arq)
    assert job.quality is Quality.P360 and job.message_id == 0  # menu gone, reaction stays
    assert m.deleted == [555] and WORKING not in m.replies


async def test_menu_without_reactions_still_becomes_placeholder(arq, deps) -> None:
    import json

    await arq.set(probe_key(YT), json.dumps([f.model_dump(mode="json") for f in MENU]))
    m = FakeMessage(YT)
    await on_text(m, bot=ReactingBot(works=False), **deps)
    assert m.replies[0] == WORKING and m.replies[1].startswith("Pick a quality")
    job_id = m.markups[0].inline_keyboard[0][0].callback_data.split(":")[1]
    q = FakeQuery(f"q:{job_id}:720p")
    q.message = await m.reply("menu")
    await on_quality_chosen(q, deps["settings"], arq, deps["limits"])
    (job,) = await _queued(arq)
    assert job.message_id == 555 and m.replies[-1] == WORKING and m.deleted == []


# ---- worker -------------------------------------------------------------------------------


def _job(**kw) -> Job:
    base = dict(
        chat_id=GROUP,
        user_id=2,
        message_id=0,
        react_message_id=12,
        url="https://x/r",
        platform=Platform.YOUTUBE,
        chat_type="supergroup",
        reply_to_message_id=12,
        thread_id=None,
    )
    return Job(**{**base, **kw})


async def _run(redis, settings, job, extractor, sender=None):
    sender = sender or FakeSender()
    await run_job(
        job,
        extractor=extractor,
        sender=sender,
        cache=FileCache(redis),
        limits=UserLimits(redis),
        redis=redis,
        settings=settings,
    )
    return sender


async def ok(job, dest, settings):
    f = dest / "v.mp4"
    f.write_bytes(b"x")
    return [f]


async def fails(job, dest, settings):
    raise ExtractError(ErrorCode.PRIVATE, "x")


async def test_worker_success_thumbs_up_and_nothing_to_delete(redis, settings) -> None:
    sender = await _run(redis, settings, _job(), ok)
    assert sender.reactions == [(GROUP, 12, REACT_DONE)]
    assert sender.finished == [] and sender.said == []


async def test_worker_failure_thumbs_down_and_short_reason(redis, settings) -> None:
    sender = await _run(redis, settings, _job(url="https://x/f"), fails)
    assert sender.reactions == [(GROUP, 12, REACT_FAILED)]
    assert sender.finished == []
    assert len(sender.said) == 1
    chat, text, reply_to, thread = sender.said[0]
    assert chat == GROUP and text.startswith("❌") and "private" in text and reply_to == 12


async def test_worker_failure_in_auto_mode_is_reaction_only(redis, settings) -> None:
    sender = await _run(redis, settings, _job(url="https://x/q", quiet=True), fails)
    assert sender.reactions == [(GROUP, 12, REACT_FAILED)] and sender.said == []


async def test_worker_menu_message_still_cleaned_up(redis, settings) -> None:
    # nobody chose and the menu couldn't be deleted: it stays the placeholder
    sender = await _run(redis, settings, _job(url="https://x/m", message_id=77), ok)
    assert sender.reactions == [(GROUP, 12, REACT_DONE)]
    assert sender.finished == [(GROUP, 77, None)]


async def test_worker_without_reaction_keeps_old_behaviour(redis, settings) -> None:
    sender = await _run(
        redis, settings, _job(url="https://x/o", react_message_id=None, message_id=77), fails
    )
    assert sender.reactions == [] and sender.said == []
    assert sender.finished[0][2].startswith("❌")


# ---- TelegramSender ---------------------------------------------------------------------


class _Bot:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list = []
        self.fail = fail

    async def set_message_reaction(self, chat_id, message_id, reaction=None, **kw):
        self.calls.append(("react", chat_id, message_id, [r.emoji for r in reaction]))
        if self.fail:
            raise TelegramBadRequest(method=SimpleNamespace(), message="reactions are disabled")

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(
            ("say", chat_id, text, kw.get("reply_parameters"), kw.get("message_thread_id"))
        )


async def test_sender_react_and_say(tmp_path: Path) -> None:
    bot = _Bot()
    sender = TelegramSender(bot)  # type: ignore[arg-type]
    assert await sender.react(1, 2, REACT_WORKING) is True
    assert await sender.react(1, 2, None) is True  # clear
    assert bot.calls[0] == ("react", 1, 2, [REACT_WORKING]) and bot.calls[1] == ("react", 1, 2, [])
    await sender.say(1, "hi", reply_to=2, thread_id=3)
    kind, chat, text, reply, thread = bot.calls[2]
    assert kind == "say" and reply.message_id == 2 and thread == 3
    assert await TelegramSender(_Bot(fail=True)).react(1, 2, REACT_DONE) is False  # type: ignore[arg-type]
