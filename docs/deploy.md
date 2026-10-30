# Deploying to a VPS (step 11)

One 2 vCPU / 4 GB box (Hetzner CX22 class) runs everything. Ubuntu 24.04 assumed.

## One-time setup

```bash
# on the VPS
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
sudo git clone https://github.com/kasrakamyab/media-downloader /opt/media-bot
cd /opt/media-bot && sudo chown -R $USER .
cp .env.example .env
```

Edit `.env`:

```
BOT_TOKEN=...
ADMIN_IDS=<your telegram user id>
MODE=webhook
DOMAIN=bot.example.com               # A record pointing at the VPS
WEBHOOK_URL=https://bot.example.com/webhook
WEBHOOK_SECRET=$(openssl rand -hex 24)
LOG_JSON=true
IMAGE=ghcr.io/kasrakamyab/media-bot:latest
```

Then, if the GHCR package is private, `docker login ghcr.io` once, and:

```bash
docker compose -f docker-compose.yml -f deploy/compose.prod.yml pull
docker compose -f docker-compose.yml -f deploy/compose.prod.yml up -d
curl -s https://bot.example.com/health
```

Caddy gets the certificate on first start. Send the bot a link.

## Continuous deploy

`.github/workflows/deploy.yml` builds the image, runs the tests, pushes
`ghcr.io/<owner>/media-bot:<timestamp>-<sha>` and `:latest`, then ssh-es in and runs
`compose pull && up -d`. Add these repository secrets: `DEPLOY_HOST`, `DEPLOY_USER`,
`DEPLOY_SSH_KEY`, `DEPLOY_PATH` (`/opt/media-bot`). A push to `main` is live in about
2–3 minutes. The same workflow runs every Monday so yt-dlp and gallery-dl stay current.

Rollback is one command — pin the previous tag and re-up:

```bash
sed -i 's|^IMAGE=.*|IMAGE=ghcr.io/kasrakamyab/media-bot:<previous-tag>|' .env
docker compose -f docker-compose.yml -f deploy/compose.prod.yml up -d
```

(`docker images ghcr.io/kasrakamyab/media-bot` lists the tags still on the box.)

## Backups

`deploy/backup-redis.sh` takes a Redis snapshot and copies it off-box with scp or rclone.
Cron it daily and set `BACKUP_DEST`. Losing Redis only loses the `file_id` cache and
counters; the bot keeps working.

## Large files (step 9)

The public Bot API caps uploads at 50 MB. To go to 2 GB, run the self-hosted Bot API server:

1. Get `api_id` / `api_hash` at https://my.telegram.org → API development tools.
2. In `.env`: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_API_URL=http://telegram-bot-api:8081`,
   `MAX_FILE_MB=500`.
3. Log the bot out of the public server once: `curl https://api.telegram.org/bot<TOKEN>/logOut`
   (you can't use both at the same time).
4. `docker compose --profile bigfiles -f docker-compose.yml -f deploy/compose.prod.yml up -d`.

Workers upload to the local server over HTTP; nothing else changes.

## Logs and alerts (step 12)

- The bot messages every id in `ADMIN_IDS` when a platform goes down (breaker open or no
  heartbeat), when it comes back, and when a breaker has been open longer than
  `ALERT_AFTER_OPEN_S` (30 min). `/stats` shows queue depth, success/error counts, cache hit
  rate and breaker state per platform.
- Logs are JSON lines on stdout (`LOG_JSON=true`). To ship them to a free tier, point a
  collector at the Docker log files, e.g. Better Stack's Vector install
  (`curl -sSL https://logs.betterstack.com/setup-vector/docker/<token> | bash`) or Grafana
  Alloy with the Docker source. Nothing in the services needs to change.
- `/health` on every container: gateway on 8080 (`platforms.*.alive`, `breaker_open`),
  workers on 8081. Compose restarts a container that fails its healthcheck.

## Troubleshooting

**Telegram is blocked on this network.** Set `TELEGRAM_PROXY` in `.env` to an HTTP or
SOCKS5 proxy the containers can reach, e.g. `socks5://host.docker.internal:10808` for a
proxy listening on the host. The gateway and workers use it for every Bot API call. Use
`DOWNLOAD_PROXY` separately if YouTube/Instagram/Pinterest are blocked too. Never set
`HTTP_PROXY`/`HTTPS_PROXY` in `.env`: they hit Redis and the healthchecks as well.

**`.env` values look wrong (`#%20http://…` in an error).** Docker's env_file is not shell:
an inline `# comment` becomes part of the value. Keep comments on their own lines. Settings
strip the common cases, but the fix is the file.

**YouTube: "Sign in to confirm you're not a bot".** Two different walls look the same:

- *Residential IP, occasional wall*: the `pot-provider` sidecar (bgutil) handles it. It
  mints proof-of-origin tokens and the yt-dlp plugin attaches them; no account needed.
  Check `docker compose ps pot-provider`.
- *Data-center or VPN exit IP* (any cloud box, most VPNs): YouTube rejects the very first
  player request by IP, before any token is asked for — `docker compose logs pot-provider`
  shows no requests at all. PO tokens cannot help here. **You need `YT_COOKIES_FILE`** from
  a throwaway Google account (export steps below). Mount it read-only as usual; the worker
  hands yt-dlp a private writable copy per job, so `./secrets` stays untouched.

The image ships `deno`, which yt-dlp needs for YouTube's JavaScript challenges; a "No
supported JavaScript runtime" warning means an old image — rebuild.

**"That platform wants a login" (`LOGIN_REQUIRED`).** **Instagram needs `IG_COOKIES_FILE`
on any server.** Without a session the worker tries yt-dlp (public reels sometimes work),
gallery-dl (login wall) and the public embed page, but from data-center and VPN IPs
Instagram serves a generic login shell with no media for every post (verified live: 200 OK,
~500 KB, identical for all posts, logged as `embed_shell_no_media`), so photo posts and
carousels only work with cookies — the same situation as YouTube behind a data-center IP.
The admin gets a 🔑 alert naming the variable to set. Fix: export cookies from a browser
where a throwaway account is logged in (any "Get cookies.txt" extension, Netscape format),
put the file in `./secrets/` and set `IG_COOKIES_FILE=/secrets/instagram.txt` (or
`YT_COOKIES_FILE`). Use an account you don't care about; export from a private window and
don't keep using that session in the browser, or the cookies rotate and expire. When the
cookies die, the same 🔑 alert tells you.

**"The upload to Telegram timed out" (`UPLOAD_FAILED`).** The download worked; the push
to `api.telegram.org` was too slow for the request timeout. The timeout scales with file
size (`max(60, size_mb × UPLOAD_SECONDS_PER_MB)`, capped at `UPLOAD_TIMEOUT`, defaults
8 s/MB and 600 s, i.e. ~0.13 MB/s minimum). The worker logs `upload_done` with bytes and
MB/s for every successful send, so you can see the real throughput and tune. On a VPS the
self-hosted Bot API server (step 9) uploads over the local network and makes this moot.

**"X is temporarily unavailable" but the worker is running.** The gateway checks the
worker's heartbeat (`heartbeat:<platform>` in Redis, 60 s TTL) and the breaker. Check
`docker compose ps` (restart loops), `docker compose logs worker-x`, and `/stats`. A breaker
opens after 5 transient failures in 10 min and closes itself after 15.

**Instagram worked on the laptop but not on the VPS.** Data-center IPs are blocked more
aggressively. Cookies first; a residential proxy for that worker only (`DOWNLOAD_PROXY` in
a per-service `environment:` override) second.

## Scaling (step 13)

| Signal | Move |
| --- | --- |
| `queue:youtube` depth > 20 for 5 min (see `/stats`) | `docker compose ... up -d --scale worker-youtube=2` |
| RAM > 3.2 GB | second VPS for the workers; set `REDIS_URL` to the first box |
| Instagram login walls | drop a cookies file in `secrets/` and set `IG_COOKIES_FILE=/secrets/ig.txt` |
