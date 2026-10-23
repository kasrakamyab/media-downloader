"""Gateway entrypoint. MODE=polling on a laptop, MODE=webhook behind Caddy on the server.

Run with:  python -m gateway.main
"""

from __future__ import annotations

import asyncio

from aiogram import Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from arq.connections import RedisSettings, create_pool

from core.cache import FileCache
from core.limits import UserLimits
from core.logging import configure_logging, get_logger
from core.settings import get_settings
from core.telegram import make_bot
from gateway.handlers import router

log = get_logger("gateway")


async def build() -> tuple[Dispatcher, object]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is not set")

    arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    dp = Dispatcher()
    dp.include_router(router)
    dp["settings"] = settings
    dp["arq"] = arq
    dp["limits"] = UserLimits(arq, settings.max_active_jobs, settings.hourly_limit)
    dp["cache"] = FileCache(arq, settings.cache_ttl_days)

    @dp.shutdown()
    async def _close() -> None:
        await arq.aclose()

    return dp, settings


async def run_polling() -> None:
    dp, settings = await build()
    bot = make_bot(settings.bot_token, settings.telegram_api_url)
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("gateway_started", mode="polling")
    try:
        await dp.start_polling(bot, allowed_updates=["message"])
    finally:
        await bot.session.close()


async def _health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def run_webhook() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    dp, settings = loop.run_until_complete(build())
    bot = make_bot(settings.bot_token, settings.telegram_api_url)

    async def on_startup(_: Dispatcher) -> None:
        await bot.set_webhook(
            settings.webhook_url,
            secret_token=settings.webhook_secret or None,
            allowed_updates=["message"],
            drop_pending_updates=False,
        )
        log.info("gateway_started", mode="webhook", url=settings.webhook_url)

    async def on_shutdown(_: Dispatcher) -> None:
        await bot.session.close()

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    app.router.add_get("/health", _health)
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
