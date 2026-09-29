#!/usr/bin/env bash
# Nightly database backup (ARCHITECTURE.md §9a.3.5).
#
# Losing the database loses the recruiters' decision history AND the per-claim
# provenance trail that the privacy position depends on. Two lines of cron.
#
# Install (Linux VM):
#   chmod +x scripts/backup.sh
#   crontab -e
#   15 2 * * *  cd /opt/hiring-intelligence && ./scripts/backup.sh >> var/backup.log 2>&1
#
# RESTORE IT ONCE BEFORE HAND-OFF. An unrestored backup is a rumour, not a backup.
# See RUNBOOK.md, "Restore a backup".

set -euo pipefail

BACKUP_DIR="${HI_BACKUP_DIR:-/var/backups/hiring-intelligence}"
KEEP_DAYS="${HI_BACKUP_KEEP_DAYS:-30}"
DB_URL="${DATABASE_URL:-postgresql://hi:hi@localhost:5442/hi}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="$BACKUP_DIR/hi-$STAMP.sql.gz"

echo "[$(date -Is)] dumping to $TARGET"
pg_dump "$DB_URL" | gzip > "$TARGET.partial"
# Rename only after a clean dump, so a truncated file is never mistaken for a backup.
mv "$TARGET.partial" "$TARGET"

SIZE="$(du -h "$TARGET" | cut -f1)"
echo "[$(date -Is)] wrote $TARGET ($SIZE)"

# A dump that is suspiciously small usually means the database is empty or the
# credentials are wrong — both are silent failures worth shouting about.
BYTES="$(stat -c%s "$TARGET" 2>/dev/null || stat -f%z "$TARGET")"
if [ "$BYTES" -lt 10000 ]; then
  echo "[$(date -Is)] WARNING: backup is only $BYTES bytes — check the database and credentials" >&2
fi

echo "[$(date -Is)] pruning backups older than $KEEP_DAYS days"
find "$BACKUP_DIR" -name 'hi-*.sql.gz' -mtime "+$KEEP_DAYS" -delete

# Copy to a SECOND location. A backup on the same disk as the database is not a
# backup — set HI_BACKUP_REMOTE to an rsync/rclone target and uncomment:
# rsync -a "$TARGET" "$HI_BACKUP_REMOTE/"

echo "[$(date -Is)] done"
