#!/usr/bin/env bash
# Копия базы и вложений. Служба при этом продолжает работать.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP=$(date +%F-%H%M)
OUT="$APP_DIR/backups"
mkdir -p "$OUT"

DB=$(grep -E '^DB_PATH=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d ' ' || echo data.db)
[ "${DB:0:1}" = "/" ] || DB="$APP_DIR/$DB"

# .backup даёт согласованную копию без остановки службы
sqlite3 "$DB" ".backup '$OUT/db-$STAMP.sqlite'"
tar czf "$OUT/media-$STAMP.tar.gz" -C "$APP_DIR" media 2>/dev/null || true

# держим последние 14 копий
ls -1t "$OUT"/db-*.sqlite 2>/dev/null | tail -n +15 | xargs -r rm --
ls -1t "$OUT"/media-*.tar.gz 2>/dev/null | tail -n +15 | xargs -r rm --

echo "бэкап готов: $OUT/db-$STAMP.sqlite"
