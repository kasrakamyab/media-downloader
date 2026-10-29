"""Gateway entrypoint. MODE=polling on a laptop, MODE=webhook behind Caddy on the server.

Run with:  python -m gateway.main
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from arq.connections import ArqRedis, RedisSettings, create_pool

from core.breaker import Breaker
from core.cache import FileCache
from core.contracts import Platform
from core.groups import GroupRegistry
from core.health import is_alive
from core.healthserver import start_health_server
from core.limits import UserLimits
from core.logging import configure_logging, get_logger
from core.settings import Settings, get_settings
from core.telegram import make_bot
from gateway.alerts import alert_loop
from gateway.handlers import router

log = get_logger("gateway")

# my_chat_member: added to / removed from groups. Not delivered unless asked for.
ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member"]


async def build() -> tuple[Dispatcher, Settings, Bot]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is not set")

    arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    bot = make_bot(settings.bot_token, settings.telegram_api_url, settings.telegram_proxy)
    breaker = Breaker(
        arq, settings.breaker_threshold, settings.breaker_window, settings.breaker_open_for
    )
    dp = Dispatcher()
    dp.include_router(router)
    dp["settings"] = settings
    dp["arq"] = arq
    dp["breaker"] = breaker
    dp["limits"] = UserLimits(arq, settings.max_active_jobs, settings.hourly_limit)
    dp["cache"] = FileCache(arq, settings.cache_ttl_days)
    dp["groups"] = GroupRegistry(arq)
    dp["bot_username"] = ""

    @dp.startup()
    async def _start_alerts() -> None:
        dp["alerts"] = asyncio.create_task(alert_loop(bot, arq, breaker, settings))

    @dp.startup()
    async def _resolve_username() -> None:
        # Needed to recognise "@bot <link>" in groups. Retried: the first Bot API call
        # through a proxy has been seen to time out once and then recover.
        for attempt in range(5):
            try:
                me = await bot.get_me()
                dp["bot_username"] = me.username or ""
                log.info("bot_identity", username=me.username, id=me.id)
                return
            except TelegramNetworkError as e:
                log.warning("get_me_failed", attempt=attempt + 1, error=str(e)[:100])
                await asyncio.sleep(2 * (attempt + 1))
        log.error("bot_username_unknown", hint="mentions in groups won't work until restart")

    @dp.shutdown()
    async def _close() -> None:
        task = dp.get("alerts")
        if task:
            task.cancel()
        await arq.aclose()
        await bot.session.close()

    return dp, settings, bot


def health_probe(arq: ArqRedis, breaker: Breaker):
    async def probe() -> dict[str, Any]:
        platforms = {}
        for p in Platform:
            platforms[p.value] = {
                "alive": await is_alive(arq, p),
                "breaker_open": await breaker.is_open(p),
            }
        return {"ok": bool(await arq.ping()), "service": "gateway", "platforms": platforms}

    return probe


async def run_polling() -> None:
    dp, settings, bot = await build()
    runner = None
    if settings.webhook_port:
        runner = await start_health_server(
            settings.webhook_port, health_probe(dp["arq"], dp["breaker"]), settings.webhook_host
        )
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("gateway_started", mode="polling")
    try:
        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
    finally:
        if runner:
            await runner.cleanup()


def run_webhook() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    dp, settings, bot = loop.run_until_complete(build())
    probe = health_probe(dp["arq"], dp["breaker"])

    async def on_startup(_: Dispatcher) -> None:
        await bot.set_webhook(
            settings.webhook_url,
            secret_token=settings.webhook_secret or None,
            allowed_updates=ALLOWED_UPDATES,
            drop_pending_updates=False,
        )
        log.info("gateway_started", mode="webhook", url=settings.webhook_url)

    async def health(_: web.Request) -> web.Response:
        body = await probe()
        return web.json_response(body, status=200 if body["ok"] else 503)

    dp.startup.register(on_startup)

    app = web.Application()
    app.router.add_get("/health", health)
    SimpleRequestHandler(
        dispatcher=dp, bot=bot, secret_token=settings.webhook_secret or None
    ).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    web.run_app(app, host=settings.webhook_host, port=settings.webhook_port, loop=loop)


def main() -> None:
    if get_settings().mode == "webhook":
        run_webhook()
    else:
        asyncio.run(run_polling())


if __name__ == "__main__":
    main()
