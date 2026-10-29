# Media Downloader Bot

Telegram bot that takes a YouTube, Instagram or Pinterest link and replies with the video or
pictures. One gateway, one Redis, one worker per platform; a dead worker (or an open circuit
breaker) only disables its own platform and the bot says which ones still work. Repeat links
are served from a Telegram `file_id` cache with no download.

See `Media Downloader Bot — Roadmap.md` for the plan, `docs/status.md` for where each step
stands, `docs/deploy.md` for the VPS release and `CLAUDE.md` for conventions.

## Run it on your laptop

Needs Docker with the Compose plugin and a bot token from [@BotFather](https://t.me/BotFather).

```bash
git clone https://github.com/kasrakamyab/media-downloader && cd media-downloader
cp .env.example .env
# edit .env: BOT_TOKEN=<token>; optionally ADMIN_IDS=<your user id> for /stats and alerts
make dev                        # Redis + gateway (polling) + 3 workers; Ctrl-C stops it
```

Then message the bot:

- a YouTube link → a quality menu (sizes shown); pick one or wait 60 s for the best under 48 MB
- a YouTube link followed by `audio` → audio only, no menu
- an Instagram post / reel / carousel or a Pinterest pin / board → the media, up to 10 items
- `/stats` (admins) → queue depth, success/error counts, cache hit rate, breaker state

In groups the bot acts only when asked — `/dl <link>`, or reply to a message with `/dl` —
and replies to the message that had the link. Group admins get `/settings` (auto-download,
max quality, platforms). See `docs/groups.md`.

`docker compose logs -f worker-youtube` shows the extractor at work; `docker compose stop
worker-youtube` shows the degradation message within 60 s.

## Develop

```bash
make install                    # uv venv + deps
make test                       # pytest (fakeredis, no network)
make lint                       # ruff
python scripts/loadtest.py      # 50 simulated users, needs a Redis (see docs/loadtest-report.md)
```

Without Docker: run `redis-server`, then in separate shells
`python -m gateway.main` and `arq workers.youtube.worker.WorkerSettings`
(same for `instagram` and `pinterest`). `ffmpeg` must be on the PATH for YouTube merges.

## Layout

| Path | What |
| --- | --- |
| `core/contracts.py` | `Job`, `Result`, `ProbeResult`, `Platform`, `Quality`, `ErrorCode`, queue names |
| `core/detector.py` | URL → platform + normalised URL (stable cache key) |
| `core/worker.py` | the worker pattern: cache → extract (with transient retries) → size cap → upload → cache → cleanup; breaker and heartbeat bookkeeping |
| `core/extractors.py` | yt-dlp quality ladder + format probe, gallery-dl wrapper |
| `core/breaker.py` | per-platform circuit breaker in Redis |
| `gateway/` | aiogram bot: commands, link handler, quality keyboard, admin alerts |
| `workers/<platform>/` | one `extract()` (and `probe()` for YouTube) per platform plus arq `WorkerSettings` |
| `deploy/` | prod Compose overlay, Caddyfile, Redis backup script |
| `scripts/loadtest.py` | 50-user load test with network stubs |
