"""Who the bot is, resolved at startup and readable by handlers afterwards.

aiogram copies `workflow_data` once, *before* the startup hooks run, so a value written in a
startup hook (`dp["bot_username"] = …`) never reaches handlers in polling mode. A mutable
holder stored before startup and filled in by the hook does: handlers keep the same object
and read its current attributes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError

from core.logging import get_logger

log = get_logger("gateway.identity")


@dataclass
class BotIdentity:
    id: int = 0
    username: str = ""
    # False with BotFather privacy mode on (the default): plain group messages then reach
    # the bot only in groups where it is an admin.
    can_read_all: bool = False
    resolved: bool = False


async def resolve_identity(
    bot: Bot, identity: BotIdentity, *, attempts: int = 5, delay_s: float = 2.0
) -> bool:
    """Fill `identity` from getMe. Retried: the first Bot API call through a proxy has been
    seen to time out once and then recover. Returns whether it succeeded."""
    for attempt in range(1, attempts + 1):
        try:
            me = await bot.get_me()
        except TelegramNetworkError as e:
            log.warning("get_me_failed", attempt=attempt, error=str(e)[:100])
            if attempt < attempts:
                await asyncio.sleep(delay_s * attempt)
            continue
        identity.id = me.id
        identity.username = me.username or ""
        identity.can_read_all = bool(me.can_read_all_group_messages)
        identity.resolved = True
        log.info(
            "bot_identity",
            username=identity.username,
            id=identity.id,
            can_read_all_group_messages=identity.can_read_all,
        )
        return True
    log.error("bot_identity_unknown", hint="mentions in groups won't work until restart")
    return False


def install_identity_resolver(dp: Dispatcher, bot: Bot) -> BotIdentity:
    """Register the holder in the dispatcher now and resolve it at startup."""
    identity = BotIdentity()
    dp["identity"] = identity

    @dp.startup()
    async def _resolve() -> None:
        await resolve_identity(bot, identity)

    return identity
