"""Admin alerts: the bot messages ADMIN_IDS when a platform goes down or comes back, and once
more when a breaker has been open longer than ALERT_AFTER_OPEN_S."""

from __future__ import annotations

import asyncio
import time

from aiogram import Bot
from redis.asyncio import Redis

from core.breaker import Breaker
from core.contracts import Platform
from core.health import is_alive
from core.logging import get_logger
from core.settings import Settings

log = get_logger("gateway.alerts")

LABEL = {
    Platform.YOUTUBE: "YouTube",
    Platform.INSTAGRAM: "Instagram",
    Platform.PINTEREST: "Pinterest",
}


def state_key(platform: Platform) -> str:
    return f"alert:down:{platform}"


def long_key(platform: Platform) -> str:
    return f"alert:long:{platform}"


async def check_once(redis: Redis, breaker: Breaker, settings: Settings) -> list[str]:
    """Return the alert messages that should be sent now and update the dedup state."""
    messages: list[str] = []
    for platform in Platform:
        alive = await is_alive(redis, platform)
        open_ = await breaker.is_open(platform)
        down = open_ or not alive
        was_down = bool(await redis.exists(state_key(platform)))
        if down and not was_down:
            why = "circuit breaker opened" if open_ else "worker heartbeat missing"
            messages.append(f"🔴 {LABEL[platform]} is down: {why}.")
            await redis.set(state_key(platform), "1")
        elif not down and was_down:
            messages.append(f"🟢 {LABEL[platform]} is back.")
            await redis.delete(state_key(platform), long_key(platform))
        if open_:
            opened = await breaker.opened_at(platform)
            if (
                opened
                and time.time() - opened >= settings.alert_after_open_s
                and await redis.set(long_key(platform), "1", ex=settings.breaker_open_for, nx=True)
            ):
                mins = int((time.time() - opened) / 60)
                messages.append(f"⚠️ {LABEL[platform]} breaker has been open for {mins} min.")
    return messages


async def alert_loop(bot: Bot, redis: Redis, breaker: Breaker, settings: Settings) -> None:
    admins = settings.admin_id_set
    if not admins:
        log.info("alerts_disabled", reason="ADMIN_IDS empty")
        return
    await asyncio.sleep(settings.heartbeat_ttl)  # let workers publish a first heartbeat
    while True:
        try:
            for text in await check_once(redis, breaker, settings):
                for admin in admins:
                    try:
                        await bot.send_message(admin, text)
                    except Exception:
                        log.exception("alert_send_failed", admin=admin)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("alert_check_failed")
        await asyncio.sleep(60)
