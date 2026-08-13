#!/usr/bin/env bash
# Одна строка о состоянии. Именно её агент показывает владельцу.
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=ai-sales
PORT=$(grep -E '^PORT=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d ' ' || true)
PORT=${PORT:-8000}

STATE=$(systemctl is-active $SERVICE 2>/dev/null || echo "не установлена")
HEALTH=$(curl -s -m 5 "http://127.0.0.1:$PORT/health" 2>/dev/null || true)

if [ -n "$HEALTH" ]; then
  echo "СЛУЖБА: $STATE | ЗДОРОВЬЕ: ок | $HEALTH"
  exit 0
fi

echo "СЛУЖБА: $STATE | ЗДОРОВЬЕ: не отвечает на порту $PORT"
echo "Последние строки лога:"
journalctl -u $SERVICE -n 10 --no-pager 2>/dev/null | tail -10
exit 1
