#!/usr/bin/env bash
# Вернуть базу из копии. Служба останавливается на время подмены.
#
#   bash deploy/restore.sh backups/db-2026-08-13-1200.sqlite
set -euo pipefail

SRC="${1:-}"
if [ -z "$SRC" ] || [ ! -f "$SRC" ]; then
  echo "укажите файл копии: bash deploy/restore.sh backups/db-ДАТА.sqlite"
  ls -1t backups/db-*.sqlite 2>/dev/null | head -5
  exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ "$(id -u)" -ne 0 ] && SUDO=sudo || SUDO=""
DB=$(grep -E '^DB_PATH=' "$APP_DIR/.env" | cut -d= -f2 | tr -d ' ')
[ "${DB:0:1}" = "/" ] || DB="$APP_DIR/$DB"

$SUDO systemctl stop ai-sales
# WAL и shm от старой базы к новой не подходят
rm -f "$DB-wal" "$DB-shm"
cp "$SRC" "$DB"
$SUDO systemctl start ai-sales
sleep 3
bash "$APP_DIR/deploy/status.sh"
