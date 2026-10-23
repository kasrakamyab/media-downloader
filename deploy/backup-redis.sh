#!/usr/bin/env bash
# Daily Redis snapshot copied off-box. Cron on the VPS:
#   17 3 * * * /opt/media-bot/deploy/backup-redis.sh >> /var/log/media-bot-backup.log 2>&1
# Set BACKUP_DEST to an rsync/scp target (user@host:/path) or an rclone remote (remote:bucket).
set -euo pipefail
cd "$(dirname "$0")/.."

DEST="${BACKUP_DEST:?set BACKUP_DEST}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="/tmp/redis-${STAMP}.rdb"

docker compose exec -T redis redis-cli --no-auth-warning BGSAVE >/dev/null
# wait for the background save to finish
until [ "$(docker compose exec -T redis redis-cli --no-auth-warning LASTSAVE)" != "${LASTSAVE:-}" ] && \
      docker compose exec -T redis redis-cli --no-auth-warning INFO persistence | grep -q 'rdb_bgsave_in_progress:0'; do
  sleep 1
done
docker compose cp redis:/data/dump.rdb "$OUT"
gzip -f "$OUT"

if command -v rclone >/dev/null && [[ "$DEST" == *:* && "$DEST" != *@* ]]; then
  rclone copy "${OUT}.gz" "$DEST"
else
  scp -q "${OUT}.gz" "$DEST"
fi
rm -f "${OUT}.gz"
echo "$(date -Is) backed up ${OUT}.gz to ${DEST}"
