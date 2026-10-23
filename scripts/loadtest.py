"""Push N concurrent jobs through the real worker code path with the network stubbed out.

Uses a real Redis (REDIS_URL) for cache, limits, breaker and stats, a stub extractor that
sleeps and writes a dummy file, and FakeSender in place of Telegram. Reports p50/p95 latency,
throughput and peak RSS. Run from the repo root:

    REDIS_URL=redis://localhost:6379/5 python scripts/loadtest.py --users 50 --concurrency 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import resource
import statistics
import time
from pathlib import Path

from redis.asyncio import Redis

from core.breaker import Breaker
from core.cache import FileCache
from core.contracts import Job, Platform
from core.limits import UserLimits
from core.logging import configure_logging
from core.settings import Settings
from core.telegram import FakeSender
from core.worker import run_job


def _rss_mb() -> float:
    kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return kb / 1024 if os.uname().sysname == "Linux" else kb / (1024 * 1024)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=50)
    ap.add_argument("--concurrency", type=int, default=3, help="jobs in flight, like a worker")
    ap.add_argument("--file-mb", type=float, default=5.0, help="size of the stub download")
    ap.add_argument("--download-s", type=float, default=2.0, help="stub download duration")
    ap.add_argument("--repeat-ratio", type=float, default=0.3, help="share of repeat URLs")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    configure_logging("WARNING")  # job_done lines would drown the report

    settings = Settings(
        BOT_TOKEN="1:x",
        JOB_DIR=os.environ.get("JOB_DIR", "/tmp/jobs-loadtest"),
        MAX_FILE_MB=max(int(args.file_mb) + 1, 1),
        MIN_FREE_GB=0,
        TRANSIENT_RETRIES=0,
        _env_file=None,
    )
    redis = Redis.from_url(settings.redis_url)
    await redis.flushdb()
    cache = FileCache(redis)
    limits = UserLimits(redis, max_active=10**6, hourly=10**6)
    breaker = Breaker(redis)
    sender = FakeSender()
    sem = asyncio.Semaphore(args.concurrency)
    payload = os.urandom(int(args.file_mb * 1024 * 1024))

    async def extractor(job: Job, dest: Path, _: Settings) -> list[Path]:
        await asyncio.sleep(args.download_s * random.uniform(0.7, 1.3))
        f = dest / "video.mp4"
        await asyncio.to_thread(f.write_bytes, payload)
        return [f]

    distinct = max(1, int(args.users * (1 - args.repeat_ratio)))
    urls = [f"https://www.youtube.com/watch?v=video{i:05d}" for i in range(distinct)]
    latencies: list[float] = []

    async def one(i: int) -> None:
        job = Job(
            chat_id=i, user_id=i, message_id=i, url=random.choice(urls), platform=Platform.YOUTUBE
        )
        async with sem:
            t = time.perf_counter()
            result = await run_job(
                job,
                extractor=extractor,
                sender=sender,
                cache=cache,
                limits=limits,
                redis=redis,
                settings=settings,
                breaker=breaker,
            )
            latencies.append(time.perf_counter() - t)
            assert result.status == "ok", result

    started = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(args.users)))
    wall = time.perf_counter() - started

    stats = await redis.hgetall("stats:youtube")
    leftovers = await asyncio.to_thread(lambda: list(Path(settings.job_dir).glob("*")))
    report = {
        "users": args.users,
        "concurrency": args.concurrency,
        "file_mb": args.file_mb,
        "wall_s": round(wall, 2),
        "jobs_per_s": round(args.users / wall, 2),
        "p50_s": round(statistics.median(latencies), 3),
        "p95_s": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 3),
        "max_s": round(max(latencies), 3),
        "cache_hits": int(stats.get(b"cache_hit", 0)),
        "peak_rss_mb": round(_rss_mb(), 1),
        "temp_leftovers": len(leftovers),
    }
    await redis.aclose()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for k, v in report.items():
            print(f"{k:>15}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
