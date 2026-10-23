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

## Scaling (step 13)

| Signal | Move |
| --- | --- |
| `queue:youtube` depth > 20 for 5 min (see `/stats`) | `docker compose ... up -d --scale worker-youtube=2` |
| RAM > 3.2 GB | second VPS for the workers; set `REDIS_URL` to the first box |
| Instagram login walls | drop a cookies file in `secrets/` and set `IG_COOKIES_FILE=/secrets/ig.txt` |
