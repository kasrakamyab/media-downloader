# Deploying to a VPS (step 11)

One 2 vCPU / 4 GB box (Hetzner CX22/CX23 class) runs everything. Ubuntu 24.04 assumed.
Memory plan on 4 GB: `worker-youtube` 1.5 GB (deno solves YouTube's JS challenges inside
each job; below ~1 GB it gets OOM-killed and downloads fail), the other two workers ~0.6 GB,
gateway + Redis + Caddy + pot-provider ~0.7 GB, the rest for the OS and page cache.

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

**No domain?** Use [sslip.io](https://sslip.io): for a server at 188.245.13.109 set
`DOMAIN=188-245-13-109.sslip.io` and `WEBHOOK_URL=https://188-245-13-109.sslip.io/webhook`.
It resolves to the IP with no setup and Caddy gets a Let's Encrypt certificate for it like
for any other name. (Polling mode works too and needs no domain at all; webhook mode just
reacts faster and avoids one long-poll connection.)

**Cookies:** `./secrets` is read inside the containers by the `bot` user (uid 1000). Make the
folder and files readable by it — `sudo chown -R 1000:1000 secrets && chmod 700 secrets &&
chmod 600 secrets/*` — or the workers log `cookies_file_missing` / permission errors.

The image is pulled from the GitHub Container Registry. **Make the package public once**
(github.com → your profile → Packages → `media-bot` → Package settings → Change visibility →
Public); after that neither the server nor the deploy workflow needs `docker login`. For a
private package, `docker login ghcr.io` on the server with a token that has `read:packages`.

```bash
docker compose -f docker-compose.yml -f deploy/compose.prod.yml pull
docker compose -f docker-compose.yml -f deploy/compose.prod.yml up -d
curl -s https://$DOMAIN/health          # through Caddy; the gateway port isn't published
```

Caddy gets the certificate on first start. Send the bot a link.

## Continuous deploy

`.github/workflows/deploy.yml` builds the image, runs the tests, pushes
`ghcr.io/<owner>/media-bot:<timestamp>-<sha>` and `:latest`, then ssh-es in and runs
`compose pull && up -d`. Add these repository secrets: `DEPLOY_HOST`, `DEPLOY_USER`,
`DEPLOY_SSH_KEY`, `DEPLOY_PATH` (`/opt/media-bot`). Without `DEPLOY_HOST` the workflow only
builds and pushes the image. A push to `main` is live in about 2–3 minutes; the workflow
checks the gateway's `/health` from inside its container (in prod the port isn't published
on the host) and prints the gateway log if that fails. The same workflow runs every Monday
so yt-dlp and gallery-dl stay current.

The server checkout is reset to `origin/main` on every deploy; untracked files such as a
local `compose.server.yml` survive that, tracked files you edited on the box do not.

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

**"That platform wants a login" (`LOGIN_REQUIRED`).** Without a session the Instagram
worker tries yt-dlp (reels), gallery-dl (usually a login wall), then two cookie-less
fallbacks: the **post page's `og:image`/`og:video`** (a plain logged-out GET of
`instagram.com/p/<id>/`). That page was served from a VPN exit where everything else was
walled, **but its og:image is a 640 px square crop** of the real photo (the full-size image
is loaded by client-side JS), so the bot sends it as a *preview* with a caption saying so,
never caches it, and raises the 🔑 alert; carousels give their first item only (logged as
`og_first_item_only`, `og_preview_cropped`). Then the embed page (full carousels from residential IPs; from
data-center / VPN exits a generic login shell, logged as `embed_shell_no_media`). Whether
the og path works depends on the exit IP, so **on a server plan for `IG_COOKIES_FILE`** —
the same situation as YouTube behind a data-center IP. The admin gets a 🔑 alert naming the
variable to set. Fix: a cookies file from a throwaway account, exported **this way** (the
order matters for YouTube):

1. Open a private/incognito window, log in to the throwaway account.
2. Export the cookies with any "Get cookies.txt" extension (Netscape format).
3. **Close the incognito window right away** and don't log in with that account anywhere
   else. A browser that stays logged in rotates the session cookies, and yt-dlp then reports
   "The provided YouTube account cookies are no longer valid".
4. Put the file in `./secrets/` (owned by uid 1000, see above) and set
   `YT_COOKIES_FILE=/secrets/youtube.txt` / `IG_COOKIES_FILE=/secrets/instagram.txt`.
5. Use the cookies from the server only. Testing the same export from another IP (your
   laptop, a VPN) while the server uses it looks like a stolen session and gets it revoked.

The site keeps rotating the session while the bot uses it (YouTube refreshes
`__Secure-*PSIDTS` every few hours), and a jar that never takes those rotations is revoked
within hours. The workers keep them in a writable copy on the `cookie-state` volume
(`COOKIE_STATE_DIR=/state`, set in `docker-compose.yml`): each job starts from that copy and
merges back the cookies the site changed. `./secrets` stays read-only. Dropping a newer
export into `./secrets` replaces the copy on the next job (log: `cookie_state_seeded`);
`cookie_rotations_saved` means a rotation was kept.

When the cookies die anyway, the same 🔑 alert tells you; repeat the steps.

**YouTube: "Requested format is not available" / only storyboards, and `dmesg` shows
`Killed process (deno)`.** The YouTube worker runs deno to solve YouTube's JavaScript
challenges; when the container's memory limit kills it, yt-dlp is left with storyboard images
only. The worker idles at ~165 MB and each job's deno run needs a few hundred MB more, so
`worker-youtube` ships with `mem_limit: 1536m` and `YT_CONCURRENCY=2` (plan ~1.5 GB of the
box for it). The job's error detail now says "YouTube JS challenge solver failed … usually
OOM-killed" and the worker logs `js_runtime_failed`; if you see it, raise the limit or lower
the concurrency rather than looking at formats.

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
