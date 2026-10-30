"""Groups: a bare link is downloaded by default where the bot can see messages."""

from __future__ import annotations

import pytest
from arq.connections import ArqRedis
from arq.jobs import deserialize_job

from core.breaker import Breaker
from core.cache import FileCache, url_hash
from core.contracts import QUEUE_NAMES, Job, Platform, Quality
from core.groups import GroupRegistry, GroupSettings
from core.health import beat
from core.limits import UserLimits
from gateway.handlers import PROMOTED_TEXT, group_help, on_help, on_text, probe_key
from gateway.identity import BotIdentity
from tests.fakes import FakeBot, FakeMessage

YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
PIN = "https://pin.it/3pihJXkrf"
GROUP = -100123
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
    await arq.set(probe_key(YT), "[]")
    groups = GroupRegistry(arq)
    await groups.add(GROUP, "videos", "supergroup", 7)
    await groups.set_visibility(GROUP, True)  # the bot is an admin there
    return {
        "settings": settings,
        "arq": arq,
        "limits": UserLimits(arq, 5, 100, chat_max_active=10, chat_hourly=100),
        "cache": FileCache(arq),
        "breaker": Breaker(arq),
        "groups": groups,
        "identity": BotIdentity(id=1, username=BOT, resolved=True),
    }


async def _queued(arq: ArqRedis) -> list[Job]:
    out = []
    for platform in Platform:
        for jid in await arq.zrange(QUEUE_NAMES[platform], 0, -1):
            raw = await arq.get(f"arq:job:{jid.decode()}")
            out.append(Job.model_validate(deserialize_job(raw).args[0]))
    return out


async def test_plain_link_in_group_with_admin_bot_is_downloaded(arq, deps) -> None:
    m = FakeMessage(PIN, chat_id=GROUP, chat_type="supergroup")
    await on_text(m, **deps)
    (job,) = await _queued(arq)
    assert job.platform is Platform.PINTEREST and job.quiet and job.message_id == 0
    assert job.reply_to_message_id == m.message_id
    assert m.replies == [] and m.answers == []  # nothing but the media later


async def test_youtube_plain_link_uses_cap_without_menu(arq, deps) -> None:
    await deps["groups"].save_settings(GROUP, GroupSettings(max_quality=Quality.P480))
    m = FakeMessage(f"look {YT}", chat_id=GROUP, chat_type="supergroup")
    await on_text(m, **deps)
    (job,) = await _queued(arq)
    assert job.quality is Quality.P480 and m.markups == []
    await deps["groups"].save_settings(GROUP, GroupSettings())  # no cap: best under size cap
    m2 = FakeMessage(f"{YT}", chat_id=GROUP, chat_type="supergroup")
    await arq.delete(f"recent:{GROUP}:{url_hash(YT)}")  # same link again: bypass the dedupe
    await on_text(m2, **deps)
    assert (await _queued(arq))[-1].quality is Quality.BEST


async def test_plain_link_where_bot_cannot_see_is_ignored(arq, deps) -> None:
    await deps["groups"].set_visibility(GROUP, False)  # plain member, privacy on
    m = FakeMessage(PIN, chat_id=GROUP, chat_type="supergroup")
    await on_text(m, **deps)
    assert await _queued(arq) == [] and m.replies == []
    # /dl still works there
    m2 = FakeMessage(f"/dl {PIN}", chat_id=GROUP, chat_type="supergroup")
    await on_text(m2, **deps)
    assert len(await _queued(arq)) == 1
    # privacy off globally overrides the stale flag
    deps["identity"].can_read_all = True
    m3 = FakeMessage("https://pin.it/other1", chat_id=GROUP, chat_type="supergroup")
    await on_text(m3, **deps)
    assert len(await _queued(arq)) == 2


async def test_unknown_group_record_allows_auto(arq, deps) -> None:
    """Groups joined before this feature have no can_see flag: if the message arrived, we
    can see it."""
    await deps["groups"].remove(GROUP)
    m = FakeMessage(PIN, chat_id=GROUP, chat_type="supergroup")
    await on_text(m, **deps)
    assert len(await _queued(arq)) == 1


async def test_links_from_bots_are_ignored(arq, deps) -> None:
    m = FakeMessage(PIN, chat_id=GROUP, chat_type="supergroup")
    m.from_user.is_bot = True  # e.g. another downloader bot re-posting the link
    await on_text(m, **deps)
    assert await _queued(arq) == []
    m2 = FakeMessage(f"/dl {PIN}", chat_id=GROUP, chat_type="supergroup")
    m2.from_user.is_bot = True
    await on_text(m2, **deps)
    assert await _queued(arq) == [] and m2.replies == []


async def test_same_link_twice_within_window_downloads_once(arq, deps) -> None:
    for user in (1, 2):
        m = FakeMessage(f"omg {PIN}", user_id=user, chat_id=GROUP, chat_type="supergroup")
        await on_text(m, **deps)
    assert len(await _queued(arq)) == 1
    # a different link goes through, and an explicit /dl is never deduped
    await on_text(
        FakeMessage("https://pin.it/other2", chat_id=GROUP, chat_type="supergroup"), **deps
    )
    await on_text(
        FakeMessage(f"/dl {PIN}", user_id=3, chat_id=GROUP, chat_type="supergroup"), **deps
    )
    assert len(await _queued(arq)) == 3
    ttl = await arq.ttl(f"recent:{GROUP}:{url_hash(PIN)}")
    assert 0 < ttl <= deps["settings"].auto_dedupe_seconds


async def test_settings_can_turn_auto_off(arq, deps) -> None:
    await deps["groups"].save_settings(GROUP, GroupSettings(auto=False))
    m = FakeMessage(PIN, chat_id=GROUP, chat_type="supergroup")
    await on_text(m, **deps)
    assert await _queued(arq) == []
    await on_text(FakeMessage(f"@{BOT} {PIN}", chat_id=GROUP, chat_type="supergroup"), **deps)
    assert len(await _queued(arq)) == 1  # explicit asks keep working


async def test_private_chat_unaffected(arq, deps) -> None:
    for _ in range(2):
        await on_text(FakeMessage(PIN, chat_id=7), **deps)  # no dedupe in private chats
    jobs = await _queued(arq)
    assert len(jobs) == 2 and not jobs[0].quiet


def test_greeting_texts() -> None:
    admin = group_help(BOT, can_see=True, joined=True)
    assert (
        "just post a link and I'll grab it" in admin
        and "/settings turns auto-download off" in admin
    )
    off = group_help(BOT, can_see=True, auto=False)
    assert "Auto-download is off" in off and "just post a link" not in off
    member = group_help(BOT, can_see=False)
    assert "Make me an admin</b> and I'll grab every link" in member
    assert "• <code>@" not in member
    assert "just post a link and I'll grab it" in PROMOTED_TEXT.format(bot=BOT)


async def test_help_reflects_auto_setting(arq, deps, settings) -> None:
    m = FakeMessage("/help", chat_id=GROUP, chat_type="supergroup")
    await on_help(
        m, settings, FakeBot(admin_in={GROUP}), identity=deps["identity"], groups=deps["groups"]
    )
    assert "just post a link" in m.replies[0]
    await deps["groups"].save_settings(GROUP, GroupSettings(auto=False))
    m2 = FakeMessage("/help", chat_id=GROUP, chat_type="supergroup")
    await on_help(
        m2, settings, FakeBot(admin_in={GROUP}), identity=deps["identity"], groups=deps["groups"]
    )
    assert "Auto-download is off" in m2.replies[0]
