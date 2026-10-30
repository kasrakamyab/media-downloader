"""Webhook mode's startup hook must work the way aiogram calls hooks: keyword args only."""

from __future__ import annotations

from aiogram import Dispatcher

from gateway.main import ALLOWED_UPDATES, webhook_startup
from tests.fakes import FakeBot


class WebhookBot(FakeBot):
    def __init__(self) -> None:
        super().__init__()
        self.webhooks: list[dict] = []

    async def set_webhook(self, url, **kw) -> None:
        self.webhooks.append({"url": url, **kw})


async def test_startup_hook_runs_through_the_dispatcher(settings) -> None:
    settings.webhook_url = "https://188-245-13-109.sslip.io/webhook"
    settings.webhook_secret = "s3cret"
    dp = Dispatcher()
    bot = WebhookBot()
    dp.startup.register(webhook_startup(bot, settings))
    await dp.emit_startup(bot=bot, dispatcher=dp)  # the crash was here: positional `_`
    (hook,) = bot.webhooks
    assert hook["url"] == settings.webhook_url
    assert hook["secret_token"] == "s3cret"
    assert hook["allowed_updates"] == ALLOWED_UPDATES
    assert hook["drop_pending_updates"] is False


async def test_empty_secret_is_sent_as_none(settings) -> None:
    settings.webhook_url = "https://x/webhook"
    settings.webhook_secret = ""
    bot = WebhookBot()
    await webhook_startup(bot, settings)(bot=bot, dispatcher=None, extra="ignored")
    assert bot.webhooks[0]["secret_token"] is None
