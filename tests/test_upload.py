"""Telegram upload timeouts and the UPLOAD_FAILED path."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramNetworkError

from core.cache import FileCache
from core.contracts import ErrorCode, Job, Platform, Quality
from core.limits import UserLimits
from core.telegram import DeliveryError, FakeSender, TelegramSender, upload_timeout_for
from core.worker import run_job

MB = 1024 * 1024


def test_upload_timeout_scales_with_size() -> None:
    assert upload_timeout_for(1 * MB, 8, 600) == 60  # floor
    assert upload_timeout_for(10 * MB, 8, 600) == 80
    assert upload_timeout_for(48 * MB, 8, 600) == 384
    assert upload_timeout_for(200 * MB, 8, 600) == 600  # cap
    assert upload_timeout_for(48 * MB, 2, 600) == 96  # fast server tuning


class _Bot:
    """Records request_timeout per call; optionally times out."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, int | None]] = []
        self.fail = fail

    async def _send(self, name, *a, **kw):
        self.calls.append((name, kw.get("request_timeout")))
        if self.fail:
            raise TelegramNetworkError(method=SimpleNamespace(), message="Request timeout error")
        msg = SimpleNamespace(video=SimpleNamespace(file_id=f"{name}-id"), photo=None)
        return [msg] * len(kw["media"]) if name == "send_media_group" else msg

    def __getattr__(self, name):
        if name.startswith("send_"):
            return lambda *a, **kw: self._send(name, *a, **kw)
        raise AttributeError(name)


async def test_send_video_uses_scaled_timeout(tmp_path: Path) -> None:
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x" * (20 * MB))
    bot = _Bot()
    sender = TelegramSender(bot, upload_timeout=600, upload_seconds_per_mb=8)  # type: ignore[arg-type]
    ids = await sender.send_files(1, [f], None)
    assert ids == ["send_video-id"]
    assert bot.calls == [("send_video", 160)]


async def test_media_group_timeout_uses_total_size(tmp_path: Path) -> None:
    files = []
    for i in range(3):
        p = tmp_path / f"{i}.jpg"
        p.write_bytes(b"x" * (5 * MB))
        files.append(p)
    bot = _Bot()
    sender = TelegramSender(bot, 600, 8)  # type: ignore[arg-type]
    ids = await sender.send_files(1, files, None)
    assert len(ids) == 3
    assert bot.calls == [("send_media_group", 120)]  # 15 MB total × 8 s/MB


async def test_network_timeout_becomes_delivery_error(tmp_path: Path) -> None:
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x" * 10)
    sender = TelegramSender(_Bot(fail=True), 600, 8)  # type: ignore[arg-type]
    with pytest.raises(DeliveryError) as e:
        await sender.send_files(1, [f], None)
    assert "Request timeout error" in str(e.value)


class TimingOutSender(FakeSender):
    async def send_files(self, chat_id, files, caption):
        raise DeliveryError("TelegramNetworkError: Request timeout error after 60s, 30000000 B")


async def test_job_reports_upload_failed(redis, settings) -> None:
    job = Job(chat_id=1, user_id=2, message_id=3, url="https://x/y", platform=Platform.YOUTUBE)
    limits = UserLimits(redis)
    await limits.try_acquire(2)
    sender = TimingOutSender()

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
    assert result.status == "error" and result.error_code is ErrorCode.UPLOAD_FAILED
    assert "lower quality" in sender.finished[0][2]
    assert await FileCache(redis).get(job.url, Quality.BEST) is None  # nothing cached
    assert await redis.get("limits:active:2") is None  # slot released
    assert await redis.hget("stats:youtube", "error:UPLOAD_FAILED") == b"1"
    assert await redis.zcard("breaker:fail:youtube") == 0  # not the platform's fault
