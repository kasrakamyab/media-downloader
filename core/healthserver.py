"""Tiny HTTP health endpoint every service exposes for Compose healthchecks and uptime pings."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

Probe = Callable[[], Awaitable[dict[str, Any]]]


async def start_health_server(port: int, probe: Probe, host: str = "0.0.0.0") -> web.AppRunner:
    """Serve GET /health -> JSON from `probe()`. Returns the runner; call `cleanup()` to stop."""

    async def health(_: web.Request) -> web.Response:
        try:
            body = await probe()
        except Exception as e:  # a broken probe must still answer, as unhealthy
            return web.json_response({"ok": False, "error": str(e)}, status=503)
        return web.json_response(body, status=200 if body.get("ok", True) else 503)

    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner
