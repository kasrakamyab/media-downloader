"""The bot's username must reach handlers even though aiogram snapshots workflow_data
before the startup hooks run (the bug: "@bot <link>" was silently ignored in groups)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram import Dispatcher
from aiogram.exceptions import TelegramNetworkError
from arq.connections import ArqRedis

from core.breaker import Breaker
from core.cache import FileCache
from core.contracts import QUEUE_NAMES, Platform
from core.groups import GroupRegistry
from core.health import beat
from core.limits import UserLimits
from gateway.handlers import on_help, on_my_chat_member, on_text, probe_key
from gateway.identity import BotIdentity, install_identity_resolver, resolve_identity
from tests.fakes import FakeBot, FakeMessage, member_update

YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
GROUP = -100123


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
    return {
        "settings": settings,
        "arq": arq,
        "limits": UserLimits(arq, 5, 100),
        "cache": FileCache(arq),
        "breaker": Breaker(arq),
        "groups": GroupRegistry(arq),
    }


async def test_startup_snapshot_still_sees_the_resolved_username(arq, deps) -> None:
    """Mirror what Dispatcher.start_polling does: copy workflow_data, *then* emit startup,
    and hand handlers the copy."""
    dp = Dispatcher()
    bot = FakeBot()
    install_identity_resolver(dp, bot)
    snapshot = dict(dp.workflow_data)  # what start_polling captures before emit_startup
    assert snapshot["identity"].username == ""  # not resolved yet, as in production
    await dp.emit_startup(bot=bot, dispatcher=dp)
    assert snapshot["identity"].username == "Mamad_AlertsBot"  # same object, now filled

    m = FakeMessage(f"@Mamad_AlertsBot {YT}", chat_id=GROUP, chat_type="supergroup")
    await on_text(m, identity=snapshot["identity"], **deps)
    assert await arq.zcard(QUEUE_NAMES[Platform.YOUTUBE]) == 1  # the mention was recognised


async def test_unresolved_identity_falls_back_to_kwargs(arq, deps) -> None:
    m = FakeMessage(f"@Mamad_AlertsBot {YT}", chat_id=GROUP, chat_type="supergroup")
    await on_text(m, identity=BotIdentity(), bot_username="Mamad_AlertsBot", **deps)
    assert await arq.zcard(QUEUE_NAMES[Platform.YOUTUBE]) == 1
    m2 = FakeMessage(f"@Mamad_AlertsBot {YT}", chat_id=GROUP, chat_type="supergroup")
    await on_text(m2, identity=BotIdentity(), **deps)  # nothing known: mention ignored
    assert await arq.zcard(QUEUE_NAMES[Platform.YOUTUBE]) == 1


async def test_resolve_identity_retries_then_fills(redis) -> None:
    class Flaky(FakeBot):
        calls = 0

        async def get_me(self):
            Flaky.calls += 1
            if Flaky.calls < 3:
                raise TelegramNetworkError(method=SimpleNamespace(), message="timeout")
            return await super().get_me()

    identity = BotIdentity()
    bot = Flaky()
    bot.can_read_all = True
    assert await resolve_identity(bot, identity, attempts=5, delay_s=0) is True
    assert identity.resolved and identity.username == "Mamad_AlertsBot"
    assert identity.can_read_all is True and identity.id == FakeBot.id

    class Dead(FakeBot):
        async def get_me(self):
            raise TelegramNetworkError(method=SimpleNamespace(), message="down")

    dead = BotIdentity()
    assert await resolve_identity(Dead(), dead, attempts=2, delay_s=0) is False
    assert not dead.resolved and dead.username == ""


async def test_identity_drives_help_and_greeting(redis, settings) -> None:
    identity = BotIdentity(id=1, username="Mamad_AlertsBot", can_read_all=True, resolved=True)
    m = FakeMessage("/help", chat_id=GROUP, chat_type="supergroup")
    await on_help(m, settings, FakeBot(), identity=identity)
    assert "• <code>@Mamad_AlertsBot" in m.replies[0]  # privacy off: mentions promised

    groups = GroupRegistry(redis)
    bot = FakeBot()
    await on_my_chat_member(member_update(GROUP, "left", "member"), bot, groups, identity=identity)
    assert "• <code>@Mamad_AlertsBot" in bot.sent[0][1]
    assert (await groups.get(GROUP))["can_see"] == "1"
