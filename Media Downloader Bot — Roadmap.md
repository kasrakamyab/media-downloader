# Media Downloader Bot — Roadmap

Oct 16, 2026 · @kasra

## Goals & constraints

A Telegram bot that takes an Instagram, Pinterest or YouTube link and sends back the video or pictures, with a quality choice where the source offers one. Built as independent services so one broken platform never takes the others down, cheap enough to run with zero revenue, and sized for 50 concurrent users with a clean path to 500+.

| Constraint | Target |
| --- | --- |
| Platforms at launch | YouTube, Instagram, Pinterest (TikTok/Twitter later, same pattern) |
| Concurrent users | 50 at launch, 500+ without redesign |
| Isolation | One worker per platform; a dead worker only disables its own platform |
| Monthly infra budget | $5–15 (one small VPS + nothing else) |
| Dev environment | Your Mac, Docker Compose, bot in polling mode |
| Release environment | One Linux VPS, same Compose file, bot in webhook mode |
| Who codes | Opus for contracts and the gateway, cheaper agents for workers and glue, you and me manage specs and review |

The one thing that keeps costs low is reuse: Telegram gives every uploaded file a permanent `file_id`, so the second person who asks for the same video gets it in under a second with no download, no CPU and no disk.

## Architecture decisions

One gateway, one Redis, one worker per platform. The gateway never downloads anything; it validates the link, checks the cache, and drops a job on that platform's queue. Each worker owns its queue end to end: extract, download, upload to Telegram, cache the `file_id`, delete the temp file. A worker that crashes or gets blocked leaves the other queues untouched.

&#91;embedded content: service map · 1 gateway, 1 Redis, 3 workers\]

The dashed path is the cheap one: a link we have already served skips the queue entirely. Everything below the gateway can be stopped, restarted or scaled without the bot going offline.

| Decision | Choice | Why |
| --- | --- | --- |
| Language | Python 3.12 everywhere | yt-dlp and gallery-dl are Python; one stack keeps cheaper agents productive and the shared code small |
| Bot framework | aiogram 3 (async) | Mature, async, inline keyboards for quality choice, polling on Mac and webhook on server with one flag |
| Queue | Redis + arq | \~20 MB RAM, one queue name per platform, job TTLs, retries built in; swaps to Streams or RabbitMQ later without touching workers |
| YouTube extractor | yt-dlp | Format listing gives the quality menu for free |
| Instagram extractor | gallery-dl primary, yt-dlp fallback | gallery-dl handles carousels and photos; yt-dlp covers reels |
| Pinterest extractor | gallery-dl primary, yt-dlp fallback | Same reason; both read a cookies file when needed |
| Delivery | Worker sends to Telegram directly | No extra hop, no shared file service; the worker already holds the file |
| Cache | Redis: `url_hash` + quality → Telegram `file_id`, 30-day TTL | Repeat requests cost zero download and zero disk |
| Shared code | One `core` package: job/result schemas, platform detector, Telegram sender, cache, settings | Agents build each worker against a fixed contract |
| Packaging | Monorepo, Docker Compose, one Dockerfile per service | Same file on Mac and server; `docker compose up --scale worker-youtube=2` is the first scaling move |
| Large files | Bot API server self-hosted (phase 9) | Raises the 50 MB upload cap to 2 GB; added only when the demand shows up |

**Repo layout**

```
media-bot/
  core/            # shared package: contracts, detector, telegram, cache, settings
  gateway/         # aiogram bot
  workers/
    youtube/
    instagram/
    pinterest/
  deploy/          # compose files, Caddyfile, systemd unit
  docs/            # one spec .md per step, written by us, handed to agents
  tests/
  CLAUDE.md        # conventions every agent reads first
```

## How we work

Every step becomes one spec file in `docs/` that a coding agent can finish in a single session without asking questions. You bring me the step, I write the spec, you hand it to the agent, the agent returns a branch, we check it against the spec's done-criteria before merging. Contracts and the gateway go to Opus because mistakes there ripple everywhere; workers and glue go to a cheaper model because they copy an existing pattern.

**What every spec contains**

1. Goal in one sentence and what is explicitly out of scope.
2. Files the agent may create or touch (nothing else).
3. Interfaces it must honor, pasted in: the `Job` and `Result` schemas, queue names, env vars.
4. Done-criteria as commands: `pytest tests/youtube -q` passes, `docker compose up worker-youtube` starts clean, a listed URL round-trips.
5. Two or three real test URLs for the platform.
6. Budget: max tool calls or tokens, so a stuck agent stops instead of burning spend.

**Rules in `CLAUDE.md` that every agent reads first**

- Python 3.12, `ruff` for lint and format, `pydantic` for every schema, `structlog` for logs.
- No new dependencies without listing them at the top of the PR description.
- Every network call has a timeout; every download has a size cap; every temp file is deleted in a `finally`.
- Workers import only from `core`, never from each other or from `gateway`.
- One branch per spec, named `step-NN-short-name`; the PR body quotes the done-criteria and marks each one passed or not.

**Model per step**

| Step | Model | Why |
| --- | --- | --- |
| 0, 1, 2, 8 | Opus | Repo skeleton, contracts, gateway and resilience shape everything after them |
| 3 (first worker) | Opus | Sets the pattern the other two workers copy |
| 4, 5, 6, 7, 10 | Sonnet or cheaper | Copies the worker pattern, UX polish, cleanup, tests |
| 9, 11, 12 | Sonnet | Config and deploy files from a known-good template |
| Reviews | You + me | We read diffs against the spec, we never let the same agent review its own work |

## Step-by-step roadmap

Fourteen steps (0 to 13), in order; each one is shippable on its own and is the unit we hand to an agent. Steps 0 to 7 run on your Mac, step 8 onward prepares and does the server release. Tick a step only when its done-criteria pass.

1. **Step 0 — Repo skeleton and conventions** (Opus)
   - Monorepo layout above, `pyproject.toml` per service, `core` as an installable package, `CLAUDE.md`, `.env.example`, `docker-compose.yml` with `redis` and empty service stubs, `Makefile` with `make dev`, `make test`, `make lint`.
   - Done: `docker compose up` starts Redis and three stub containers that log a heartbeat and exit cleanly on `Ctrl-C`.
2. **Step 1 — Contracts in `core`** (Opus)
   - `Job` (job\_id, chat\_id, message\_id, url, platform, quality, requested\_at), `Result` (status, file\_ids, error\_code, took\_ms), `Platform` enum, error taxonomy (`UNSUPPORTED_URL`, `PRIVATE`, `TOO_LARGE`, `RATE_LIMITED`, `SOURCE_DOWN`, `UNKNOWN`), queue names, settings loader, structured logging.
   - Platform detector: regex per platform, normalises short links (`youtu.be`, `pin.it`, `instagr.am`) and strips tracking params so the cache key is stable.
   - Done: 100% test coverage on the detector with 30+ real-shaped URLs; schemas serialise to and from JSON.
3. **Step 2 — Gateway MVP** (Opus)
   - aiogram bot, polling mode behind `MODE=polling|webhook`. `/start`, `/help`, URL handler: detect platform → reply "Working on it…" → enqueue to that platform's arq queue → worker edits that message when done.
   - Per-user limits in Redis: max 2 active jobs, 20 requests per hour; a friendly message when exceeded.
   - Done: a YouTube link produces a job in `queue:youtube` (visible via `redis-cli`) and the user gets the placeholder message within 1 s.
4. **Step 3 — YouTube worker** (Opus, sets the pattern)
   - arq worker on `queue:youtube`, concurrency 3. yt-dlp with `--no-playlist`, max filesize 48 MB by default, format fallback ladder (`720p` → `480p` → `360p` → audio). Downloads to `/tmp/jobs/<job_id>/`, uploads with `send_video` or `send_document`, writes `file_id` to cache, deletes the folder in `finally`.
   - Timeouts: 30 s extract, 120 s download. On any failure: `Result` with an error code, user gets a one-line reason.
   - Done: 5 listed URLs round-trip; a second request for the same URL is served from cache with no yt-dlp call (assert via logs).
5. **Step 4 — Instagram worker** (Sonnet, copies step 3)
   - Same shape. gallery-dl first (handles photos, carousels, reels); yt-dlp as fallback for video-only posts. Carousels become a `send_media_group` of up to 10 items. Optional `IG_COOKIES_FILE` env var for private-ish content.
   - Done: a public reel, a photo post and a 3-item carousel each round-trip.
6. **Step 5 — Pinterest worker** (Sonnet, copies step 3)
   - Same shape. gallery-dl for pins and boards (boards capped at 10 items), yt-dlp fallback for video pins. Resolves `pin.it` short links before extraction.
   - Done: an image pin, a video pin and a short board each round-trip.
7. **Step 6 — Quality selection UX** (Sonnet)
   - Gateway asks the worker for a format list first (a cheap `probe` job), shows an inline keyboard (e.g. 1080p · 720p · 360p · audio) with sizes, then enqueues the real job with the chosen quality. Default to the best under the size cap if the user doesn't answer in 60 s.
   - Cache key becomes `url_hash:quality`.
   - Done: YouTube shows real formats; Instagram and Pinterest skip the keyboard (one quality).
8. **Step 7 — Resource controls and cleanup** (Sonnet)
   - Worker concurrency and job TTL from env; a `janitor` task that deletes any `/tmp/jobs/*` older than 10 min; disk guard that refuses new downloads below 1 GB free; memory cap per container in Compose (`mem_limit`); yt-dlp `--limit-rate` configurable.
   - Done: a load script with 50 simulated users (step 10) keeps RSS under 300 MB per worker and leaves `/tmp/jobs` empty afterwards.
9. **Step 8 — Resilience and graceful degradation** (Opus)
   - Circuit breaker per platform in Redis: 5 failures with `SOURCE_DOWN` in 10 min opens it for 15 min; gateway then replies "YouTube is temporarily unavailable, Instagram and Pinterest still work" without enqueuing. Heartbeat key per worker; gateway treats a missing heartbeat (60 s) the same as an open breaker. Retries with backoff for transient errors only. Health endpoint on each service.
   - Done: killing `worker-youtube` makes the bot say so within 60 s while Instagram links still succeed; restarting it closes the breaker.
10. **Step 9 — Self-hosted Bot API server** (Sonnet, optional until needed)
    - `telegram-bot-api` container; gateway and workers point `TELEGRAM_API_URL` at it. Lifts upload cap to 2 GB and removes the 20 MB download-by-URL limit. Raise the default size cap to 500 MB and add a `TOO_LARGE` message above it.
    - Done: a 150 MB YouTube video round-trips on the Mac.
11. **Step 10 — Test suite and 50-user load test on the Mac** (Sonnet)
    - Unit tests per service, a fake Telegram sender in `core` for tests, and a `scripts/loadtest.py` that pushes 50 concurrent jobs through the real queues with network stubs. Record p50/p95 latency and peak RAM per container.
    - Done: CI green on GitHub Actions; the load report is checked into `docs/`.
12. **Step 11 — Server release** (Sonnet)
    - Pick a 2 vCPU / 4 GB VPS (Hetzner CX22 class, about €4/month). `deploy/compose.prod.yml` with restart policies and log rotation, Caddy for TLS on the webhook, gateway in `MODE=webhook`, secrets in a `.env` not in git, GitHub Actions deploy by `ssh` + `docker compose pull && up -d`, a weekly job that rebuilds images so yt-dlp and gallery-dl stay current, daily Redis snapshot copied off-box.
    - Done: the bot answers from the VPS; a push to `main` redeploys in under 3 min; a rollback is one command.
13. **Step 12 — Observability and admin** (Sonnet)
    - `/stats` admin command (jobs per platform, cache hit rate, breaker states), structured logs shipped to a free tier (Grafana Cloud or Better Stack), one alert: any breaker open longer than 30 min. Nothing heavier until users justify it.
    - Done: you get a Telegram message from the bot itself when a platform goes down.
14. **Step 13 — Scaling moves, applied only when a number says so** (you and me)
    - Queue depth over 20 for 5 min → `--scale worker-X=2`. VPS CPU over 70% sustained → move workers to a second VPS (Redis stays on the first; change one env var). Instagram blocks → cookies rotation and a cheap residential proxy only for that worker. Cache misses dominate → object storage (Cloudflare R2, free 10 GB) in front of Telegram re-uploads. Each is a one-line change in Compose, none needs code rewrites.

**Order rationale.** Contracts before any worker, so three agents can build three workers against one truth. The first worker is done carefully because the next two are copies. Resilience comes before release, not after, because it is the feature you asked for first.

## Resource budget & scaling path

50 concurrent users fits on one 2 vCPU / 4 GB VPS with room to spare, because almost nothing is CPU-bound: yt-dlp mostly waits on the network, and cache hits cost nothing. The limits below are the starting values for `.env`; the load test in step 10 confirms or adjusts them.

| Service | Replicas | Concurrency | RAM cap | Notes |
| --- | --- | --- | --- | --- |
| gateway | 1 | async, unlimited | 256 MB | Only parses and enqueues; survives 500 users unchanged |
| redis | 1 | — | 128 MB | `maxmemory 100mb`, `allkeys-lru`; cache entries are \~100 bytes each |
| worker-youtube | 1 | 3 jobs | 512 MB | Heaviest: 720p video streams at a few MB/s |
| worker-instagram | 1 | 4 jobs | 384 MB | Mostly images, short videos |
| worker-pinterest | 1 | 4 jobs | 256 MB | Mostly images |
| telegram-bot-api | 1 (step 9) | — | 256 MB | Only if files over 50 MB matter |
| Disk | — | — | 20 GB | Temp files live under 10 min; 11 concurrent downloads × 50 MB ≈ 0.6 GB peak |

Total at launch: about 1.8 GB RAM and one VPS at roughly €4–6/month. Bandwidth is the real meter: a 2 vCPU Hetzner box includes 20 TB/month, which is about 400,000 videos of 50 MB, so bandwidth is not the first ceiling either.

**What scales and what it costs**

| Signal | Move | Added cost |
| --- | --- | --- |
| `queue:youtube` depth > 20 for 5 min | `docker compose up --scale worker-youtube=2` | €0 until RAM runs out |
| RAM > 3.2 GB | Move the three workers to a second VPS; point `REDIS_URL` at the first | +€4–6/month |
| 500+ users | Second gateway behind the webhook is not needed; add a third worker VPS instead | +€4–6/month |
| Instagram rate limits | Cookies rotation, then one residential proxy for that worker only | €0–10/month |
| Disk churn | Cloudflare R2 for re-serving files already downloaded once | free to 10 GB |

Nothing on this path requires Kubernetes, a managed queue or a database. If the bot ever earns money, the first upgrade worth buying is a managed Redis so the cache survives host loss; until then the daily snapshot is enough.

## Platform notes & risks

The bot's biggest risk is not load, it is the platforms changing under it; the microservice split and the weekly image rebuild are the mitigations. Everything else is a known limit with a known workaround.

| Risk | What happens | Mitigation | Step |
| --- | --- | --- | --- |
| Telegram upload cap | Bot API refuses files over 50 MB; `send_*` by URL caps at 20 MB | Size cap at 48 MB by default; self-hosted Bot API server raises it to 2 GB | 3, 9 |
| YouTube bot detection | yt-dlp starts failing with sign-in errors on the VPS | Weekly rebuild keeps yt-dlp current; a cookies file as env var; breaker opens so users hear it is YouTube, not the bot | 3, 8, 11 |
| Instagram blocks data-center IPs | Extraction fails or returns login walls | gallery-dl with a cookies file from a throwaway account; a residential proxy only for this worker; aggressive cache reuse | 4, 13 |
| Pinterest changes markup | gallery-dl breaks for pins | yt-dlp fallback; weekly rebuild; breaker | 5, 8 |
| Extractor updates break things | A new yt-dlp version regresses | Pin versions in `requirements.txt`; the weekly rebuild runs the round-trip test before replacing the live image | 11 |
| Abuse | One user floods the bot or sends playlists | Per-user limits, `--no-playlist`, boards capped at 10 items, max 2 active jobs | 2, 5, 7 |
| Disk fills | A crashed worker leaves temp files | `finally` cleanup plus the janitor plus the free-space guard | 7 |
| Token or cookies leak | Secrets committed to git | `.env` in `.gitignore`, `.env.example` only, secrets never in `CLAUDE.md` or specs | 0 |
| Legal | Users download copyrighted or private content | `/help` states personal use only; private content is refused with `PRIVATE`; honor takedown requests by blocking URL hashes in Redis | 1, 2 |

**Open questions to settle before step 0**

- [ ] Bot name and `@username` — reserve it with BotFather now, it is the only thing that cannot be renamed later.
- [ ] Default quality when the user does not choose: best under 48 MB, or always 720p?
- [ ] Instagram: are we fine starting with a throwaway-account cookies file, or public-only content at launch?
- [ ] GitHub repo visibility (private is fine; agents work from a clone).
