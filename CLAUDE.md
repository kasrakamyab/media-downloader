# Media Downloader Bot — conventions for every agent

Read this before touching anything. The roadmap lives in `Media Downloader Bot — Roadmap.md`;
each step gets one spec in `docs/` that lists the files you may touch.

## Stack
- Python 3.12, `ruff` for lint and format (`make lint`, `make fmt`), `pytest` (`make test`).
- `pydantic` for every schema, `pydantic-settings` for config, `structlog` for logs.
- aiogram 3 for the bot, arq on Redis for queues, yt-dlp and gallery-dl for extraction.

## Layout
```
core/        shared package: contracts, detector, settings, cache, limits, health, telegram,
             worker (the pattern), extractors (yt-dlp / gallery-dl wrappers)
gateway/     aiogram bot (handlers.py, main.py)
workers/     one package per platform, each is ~20 lines: an `extract()` + WorkerSettings
deploy/      prod compose overlay, Caddyfile
tests/       pytest; fakeredis stands in for Redis, FakeSender for Telegram
```

## Rules
- Workers import only from `core`, never from each other or from `gateway`.
- Every network call has a timeout; every download has a size cap; every temp file is deleted
  in a `finally` (see `core/worker.py:run_job`).
- No new dependencies without listing them at the top of the PR description.
- Never commit secrets. `.env` and `secrets/` are gitignored; `.env.example` has every key.
- One branch per spec, named `step-NN-short-name`; the PR body quotes the spec's done-criteria
  and marks each one passed or not.
- Contracts (`core/contracts.py`) change only with a spec that updates the gateway, all three
  workers and the tests in the same PR.

## Running
```
cp .env.example .env   # set BOT_TOKEN
make install && make test
make dev               # Redis + gateway + 3 workers, polling mode
```
Without Docker: `redis-server`, then `python -m gateway.main` and
`arq workers.youtube.worker.WorkerSettings` (same for instagram / pinterest).
