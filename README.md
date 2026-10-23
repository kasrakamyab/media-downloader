# Media Downloader Bot

Telegram bot that takes a YouTube, Instagram or Pinterest link and replies with the video or
pictures. One gateway, one Redis, one worker per platform; a dead worker only disables its own
platform. Repeat links are served from a Telegram `file_id` cache with no download.

See `Media Downloader Bot — Roadmap.md` for the plan and `CLAUDE.md` for conventions.

## Run it

```bash
cp .env.example .env            # put your BotFather token in BOT_TOKEN
make dev                        # Redis + gateway (polling) + 3 workers
```

Send the bot a link. Add the word `audio` after a YouTube link for sound only.

## Develop

```bash
make install                    # uv venv + deps
make test                       # pytest (fakeredis, no network)
make lint                       # ruff
```

Without Docker: run `redis-server`, then in separate shells
`python -m gateway.main` and `arq workers.youtube.worker.WorkerSettings`
(same for `instagram` and `pinterest`).

## Deploy

```bash
MODE=webhook WEBHOOK_URL=https://bot.example.com/webhook  # in .env
docker compose -f docker-compose.yml -f deploy/compose.prod.yml up -d
```

Caddy (`deploy/Caddyfile`) terminates TLS and proxies `/webhook` to the gateway.

## Layout

| Path | What |
| --- | --- |
| `core/contracts.py` | `Job`, `Result`, `Platform`, `Quality`, `ErrorCode`, queue names |
| `core/detector.py` | URL → platform + normalised URL (stable cache key) |
| `core/worker.py` | the worker pattern: cache → extract → size cap → upload → cache → cleanup |
| `core/extractors.py` | yt-dlp quality ladder and gallery-dl wrappers |
| `gateway/` | aiogram bot: `/start`, `/help`, `/stats`, link handler, per-user limits |
| `workers/<platform>/` | one `extract()` per platform plus arq `WorkerSettings` |
