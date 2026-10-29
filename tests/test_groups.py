"""Group chats: triggers, in-context replies, membership tracking, migration, rights."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from arq.connections import ArqRedis
from arq.jobs import deserialize_job

from core.breaker import Breaker
from core.cache import FileCache
from core.contracts import QUEUE_NAMES, ErrorCode, Job, Platform, Quality
from core.groups import GroupRegistry
from core.health import beat
from core.limits import UserLimits
from core.telegram import FakeSender, ForbiddenError, TelegramSender
from core.worker import run_job
from gateway.handlers import (
    WORKING,
    extract_ask,
    group_help,
    on_help,
    on_migrate,
    on_my_chat_member,
    on_text,
    probe_key,
)
from tests.fakes import FakeBot, FakeMessage, member_update

YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
BOT = "Mamad_AlertsBot"


@pytest.fixture
async def arq(redis) -> ArqRedis:
    pool = ArqRedis(connection_pool=redis.connection_pool)
    yield pool
    await pool.aclose()


@pytest.fixture
async def deps(arq, settings):
    for p in Platform:
        await beat(arq, p, 60)
    await arq.set(probe_key(YT), "[]")  # no quality menu: enqueue straight away
    return {
        "settings": settings,
        "arq": arq,
        "limits": UserLimits(arq, settings.max_active_jobs, settings.hourly_limit),
        "cache": FileCache(arq),
        "breaker": Breaker(arq),
        "bot_username": BOT,
    }


async def _queued(arq: ArqRedis, platform: Platform) -> list[Job]:
    ids = await arq.zrange(QUEUE_NAMES[platform], 0, -1)
    out = []
    for jid in ids:
        raw = await arq.get(f"arq:job:{jid.decode()}")
        out.append(Job.model_validate(deserialize_job(raw).args[0]))
    return out


# ---- triggers -------------------------------------------------------------------------


def test_private_chat_any_link_is_an_ask() -> None:
    m = FakeMessage(f"look {YT}")
    ask = extract_ask(m, BOT)
    assert ask and ask.source is m and YT in ask.text


def test_group_plain_link_is_ignored() -> None:
    assert extract_ask(FakeMessage(YT, chat_type="supergroup"), BOT) is None
    assert extract_ask(FakeMessage("hello", chat_type="group"), BOT) is None


@pytest.mark.parametrize(
    "text",
    [f"/dl {YT}", f"/dl@{BOT} {YT}", f"/DL {YT} audio", f"@{BOT} {YT}", f"hey @{BOT.lower()} {YT}"],
)
def test_group_command_and_mention(text: str) -> None:
    m = FakeMessage(text, chat_type="supergroup")
    ask = extract_ask(m, BOT)
    assert ask and ask.source is m and YT in ask.text


def test_group_reply_with_dl_uses_quoted_message() -> None:
    original = FakeMessage(f"check this {YT}", user_id=3, chat_type="supergroup")
    m = FakeMessage("/dl audio", chat_type="supergroup", reply_to=original)
    ask = extract_ask(m, BOT)
    assert ask and ask.source is original
    assert YT in ask.text and "audio" in ask.text


def test_group_reply_to_media_caption_and_hidden_link() -> None:
    original = FakeMessage(None, chat_type="supergroup", caption=f"from yt {YT}")
    assert extract_ask(FakeMessage("/dl", chat_type="supergroup", reply_to=original), BOT)
    hidden = FakeMessage(
        "/dl this",
        chat_type="supergroup",
        entities=[SimpleNamespace(type="text_link", url=YT)],
    )
    ask = extract_ask(hidden, BOT)
    assert ask and YT in ask.text


def test_group_dl_without_link_still_asks() -> None:
    ask = extract_ask(FakeMessage("/dl", chat_type="group"), BOT)
    assert ask is not None  # on_text answers with a hint


def test_unknown_username_means_no_mentions() -> None:
    assert extract_ask(FakeMessage(f"@{BOT} {YT}", chat_type="group"), "") is None


# ---- handler flow in groups -------------------------------------------------------------


async def test_group_dl_enqueues_with_reply_target(arq, deps) -> None:
    m = FakeMessage(f"/dl {YT}", chat_id=-100123, chat_type="supergroup")
    await on_text(m, **deps)
    assert m.replies == [WORKING]  # placeholder replies to the asking message
    (job,) = await _queued(arq, Platform.YOUTUBE)
    assert job.chat_id == -100123 and job.chat_type == "supergroup" and job.is_group
    assert job.reply_to_message_id == m.message_id
    assert job.thread_id is None


async def test_group_reply_dl_targets_original_message_and_topic(arq, deps) -> None:
    original = FakeMessage(YT, user_id=3, chat_id=-100123, chat_type="supergroup", thread_id=42)
    m = FakeMessage(
        "/dl", user_id=7, chat_id=-100123, chat_type="supergroup", reply_to=original, thread_id=42
    )
    await on_text(m, **deps)
    assert original.replies == [WORKING] and m.replies == []
    (job,) = await _queued(arq, Platform.YOUTUBE)
    assert job.user_id == 7  # the asker owns the job (limits, quality menu)
    assert job.reply_to_message_id == original.message_id
    assert job.thread_id == 42


async def test_group_noise_is_silent(arq, deps) -> None:
    m = FakeMessage("what a video", chat_type="supergroup")
    await on_text(m, **deps)
    assert m.answers == [] and m.replies == []
    assert await _queued(arq, Platform.YOUTUBE) == []


async def test_group_dl_without_link_hints_in_reply(arq, deps) -> None:
    m = FakeMessage("/dl", chat_type="supergroup")
    await on_text(m, **deps)
    assert m.replies and "Give me a link" in m.replies[0] and m.answers == []


async def test_private_chat_unchanged(arq, deps) -> None:
    m = FakeMessage(YT)
    await on_text(m, **deps)
    (job,) = await _queued(arq, Platform.YOUTUBE)
    assert job.chat_type == "private" and job.reply_to_message_id is None


async def test_group_help_depends_on_what_the_bot_can_see(settings) -> None:
    m = FakeMessage("/help", chat_id=-5, chat_type="group")
    await on_help(m, settings, FakeBot(), bot_username=BOT)  # plain member
    assert (
        "Make me an admin" in m.replies[0] and f"@{BOT} &lt;link&gt;</code>\n" not in m.replies[0]
    )
    assert "/dl &lt;link&gt;" in m.replies[0]

    m = FakeMessage("/help", chat_id=-5, chat_type="group")
    await on_help(m, settings, FakeBot(admin_in={-5}), bot_username=BOT)  # admin here
    assert f"• <code>@{BOT} &lt;link&gt;</code>" in m.replies[0]
    assert "auto-download" in m.replies[0] and "Make me an admin" not in m.replies[0]

    m = FakeMessage("/help", chat_id=-5, chat_type="group")
    await on_help(m, settings, FakeBot(), bot_username=BOT, bot_can_read_all=True)  # privacy off
    assert f"• <code>@{BOT} &lt;link&gt;</code>" in m.replies[0]


def test_group_help_text_shapes() -> None:
    plain = group_help(BOT, can_see=False, joined=True)
    assert plain.startswith("👋 Thanks for adding me.") and "Make me an admin" in plain
    assert "• <code>@" not in plain  # mention not promised when Telegram won't deliver it
    admin = group_help(BOT, can_see=True)
    assert not admin.startswith("👋") and f"• <code>@{BOT}" in admin


# ---- membership and migration -----------------------------------------------------------


async def test_my_chat_member_add_promote_demote_remove(redis) -> None:
    groups = GroupRegistry(redis)
    bot = FakeBot()
    await on_my_chat_member(member_update(-1, "left", "member"), bot, groups, bot_username=BOT)
    assert await groups.is_member(-1) and await groups.count() == 1
    info = await groups.get(-1)
    assert info["title"] == "Test group" and info["added_by"] == "7" and info["can_see"] == "0"
    assert bot.sent[0][1].startswith("👋") and "Make me an admin" in bot.sent[0][1]
    assert "• <code>@" not in bot.sent[0][1]

    await on_my_chat_member(
        member_update(-1, "member", "administrator"), bot, groups, bot_username=BOT
    )
    assert (await groups.get(-1))["can_see"] == "1"
    assert len(bot.sent) == 2 and "Thanks for making me an admin" in bot.sent[1][1]
    assert f"@{BOT} &lt;link&gt;" in bot.sent[1][1]

    await on_my_chat_member(member_update(-1, "administrator", "member"), bot, groups)
    assert (await groups.get(-1))["can_see"] == "0"
    assert len(bot.sent) == 3 and "lost admin rights" in bot.sent[2][1]

    await on_my_chat_member(member_update(-1, "member", "kicked"), bot, groups)
    assert not await groups.is_member(-1) and await groups.get(-1) is None


async def test_added_directly_as_admin_or_with_privacy_off(redis) -> None:
    groups = GroupRegistry(redis)
    bot = FakeBot()
    await on_my_chat_member(
        member_update(-3, "left", "administrator"), bot, groups, bot_username=BOT
    )
    assert (await groups.get(-3))["can_see"] == "1"
    assert f"• <code>@{BOT}" in bot.sent[0][1] and "Make me an admin" not in bot.sent[0][1]

    bot2 = FakeBot()
    await on_my_chat_member(
        member_update(-4, "left", "member"), bot2, groups, bot_username=BOT, bot_can_read_all=True
    )
    assert (await groups.get(-4))["can_see"] == "1" and f"• <code>@{BOT}" in bot2.sent[0][1]
    # demotion with privacy off changes nothing visible
    await on_my_chat_member(
        member_update(-4, "administrator", "member"), bot2, groups, bot_can_read_all=True
    )
    assert len(bot2.sent) == 1


async def test_my_chat_member_ignores_private_and_greeting_failure(redis) -> None:
    groups = GroupRegistry(redis)

    class MuteBot(FakeBot):
        async def send_message(self, *a, **k):
            raise TelegramForbiddenError(method=SimpleNamespace(), message="no rights")

    await on_my_chat_member(
        member_update(5, "left", "member", chat_type="private"), MuteBot(), groups
    )
    assert await groups.count() == 0
    await on_my_chat_member(member_update(-2, "left", "member"), MuteBot(), groups)
    assert await groups.is_member(-2)  # tracked even though the greeting failed


async def test_migration_moves_group_and_its_settings(redis) -> None:
    groups = GroupRegistry(redis)
    await groups.add(-1, "Old", "group", 7)
    await redis.hset("group:-1:settings", "auto", "1")  # what a later step will store
    m = FakeMessage(None, chat_id=-1, chat_type="group")
    m.migrate_to_chat_id = -100999
    await on_migrate(m, groups)
    assert not await groups.is_member(-1) and await groups.is_member(-100999)
    assert (await groups.get(-100999))["type"] == "supergroup"
    assert await redis.hget("group:-100999:settings", "auto") == b"1"
    assert await redis.exists("group:-1") == 0
    assert await groups.migrate(-1, -100999) is False  # nothing left to move


# ---- delivery in groups -----------------------------------------------------------------


class _Bot:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.error = error

    async def _send(self, name, chat_id, *a, **kw):
        self.calls.append({"method": name, **kw})
        if self.error:
            raise self.error
        return SimpleNamespace(video=SimpleNamespace(file_id="fid"), photo=None)

    def __getattr__(self, name):
        if name.startswith("send_"):
            return lambda *a, **kw: self._send(name, *a, **kw)
        raise AttributeError(name)


async def test_sender_replies_in_thread(tmp_path: Path) -> None:
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    bot = _Bot()
    sender = TelegramSender(bot)  # type: ignore[arg-type]
    await sender.send_files(-1, [f], None, reply_to=12, thread_id=42)
    call = bot.calls[0]
    assert call["method"] == "send_video"
    assert call["reply_parameters"].message_id == 12
    assert call["reply_parameters"].allow_sending_without_reply is True
    assert call["message_thread_id"] == 42

    await sender.send_cached(-1, ["fid"], None, reply_to=12)
    assert bot.calls[1]["reply_parameters"].message_id == 12
    assert "message_thread_id" not in bot.calls[1]

    await sender.send_files(5, [f], None)  # private chat: no reply, no thread
    assert "reply_parameters" not in bot.calls[2] and "message_thread_id" not in bot.calls[2]


@pytest.mark.parametrize(
    "error",
    [
        TelegramForbiddenError(method=SimpleNamespace(), message="bot was kicked from the group"),
        TelegramBadRequest(method=SimpleNamespace(), message="not enough rights to send videos"),
        TelegramBadRequest(method=SimpleNamespace(), message="CHAT_SEND_MEDIA_FORBIDDEN"),
    ],
)
async def test_rights_errors_become_forbidden(tmp_path: Path, error: Exception) -> None:
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    sender = TelegramSender(_Bot(error))  # type: ignore[arg-type]
    with pytest.raises(ForbiddenError):
        await sender.send_files(-1, [f], None)
    with pytest.raises(ForbiddenError):
        await sender.send_cached(-1, ["fid"], None)


async def test_other_bad_request_still_falls_back_to_document(tmp_path: Path) -> None:
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")

    class Picky(_Bot):
        async def _send(self, name, chat_id, *a, **kw):
            self.calls.append({"method": name})
            if name == "send_video":
                raise TelegramBadRequest(
                    method=SimpleNamespace(), message="VIDEO_CONTENT_TYPE_INVALID"
                )
            return SimpleNamespace(video=None, photo=None, document=SimpleNamespace(file_id="d"))

    bot = Picky()
    assert await TelegramSender(bot).send_files(-1, [f], None) == ["d"]  # type: ignore[arg-type]
    assert [c["method"] for c in bot.calls] == ["send_video", "send_document"]


async def test_job_in_group_delivers_to_source_and_maps_forbidden(redis, settings) -> None:
    job = Job(
        chat_id=-1,
        user_id=2,
        message_id=3,
        url="https://x/g",
        platform=Platform.YOUTUBE,
        chat_type="supergroup",
        reply_to_message_id=12,
        thread_id=42,
    )
    limits = UserLimits(redis)
    sender = FakeSender()

    async def extractor(job, dest, settings):
        f = dest / "v.mp4"
        f.write_bytes(b"x")
        return [f]

    result = await run_job(
        job,
        extractor=extractor,
        sender=sender,
        cache=FileCache(redis),
        limits=limits,
        redis=redis,
        settings=settings,
    )
    assert result.status == "ok" and sender.targets == [(12, 42)]

    class NoRights(FakeSender):
        async def send_files(self, *a, **k):
            raise ForbiddenError("not enough rights")

    job2 = job.model_copy(update={"url": "https://x/g2"})
    result = await run_job(
        job2,
        extractor=extractor,
        sender=NoRights(),
        cache=FileCache(redis),
        limits=limits,
        redis=redis,
        settings=settings,
    )
    assert result.error_code is ErrorCode.FORBIDDEN
    assert await FileCache(redis).get(job2.url, Quality.BEST) is None
