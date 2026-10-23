# Load test report — 50 simulated users (step 10)

`scripts/loadtest.py` pushes 50 jobs through the real worker code path (`core.worker.run_job`
with Redis cache, limits, breaker and stats) with the network stubbed: the extractor sleeps
~2 s and writes a 5 MB file, and `FakeSender` stands in for Telegram. 30 % of the URLs repeat,
so some requests are cache hits.

Run on 2026-10-23 in a 2-vCPU container against a local Redis 7:

| Setting | Worker-like (concurrency 3) | All workers' slots (concurrency 11) |
| --- | --- | --- |
| Users / jobs | 50 | 50 |
| Wall time | 22.7 s | 8.0 s |
| Throughput | 2.2 jobs/s | 6.3 jobs/s |
| p50 latency | 1.63 s | 1.86 s |
| p95 latency | 2.44 s | 2.50 s |
| Max latency | 2.65 s | 2.59 s |
| Cache hits | 17 / 50 | 13 / 50 |
| Peak RSS | 179 MB | 179 MB |
| `/tmp/jobs` leftovers after run | 0 | 0 |

Takeaways

- Latency is the stub download time plus a few ms of Redis and file I/O: the worker pattern
  itself adds no measurable overhead, so real latency is whatever yt-dlp/gallery-dl and the
  Telegram upload take.
- RSS stays under the 300 MB target with 11 jobs in flight; the 5 MB payload is held in
  memory once and written per job.
- Every temp folder was removed, so the `finally` cleanup holds under concurrency; the
  janitor only matters after a crash.
- Cache hits cost ~0 ms. With real traffic the hit rate is the number that decides capacity.

Reproduce:

```bash
redis-server --port 6399 --daemonize yes
REDIS_URL=redis://localhost:6399/5 python scripts/loadtest.py --users 50 --concurrency 3
REDIS_URL=redis://localhost:6399/5 python scripts/loadtest.py --users 50 --concurrency 11
```
