#!/usr/bin/env bash
# Одна строка о состоянии. Именно её агент показывает владельцу.
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=ai-sales
PORT=$(grep -E '^PORT=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d ' ' || true)
PORT=${PORT:-8000}

STATE=$(systemctl is-active $SERVICE 2>/dev/null || echo "не установлена")

# Служба поднимается 5–8 секунд: боты выходят на связь, планировщик стартует.
# Фиксированная пауза давала ложное «не отвечает» после каждого деплоя, поэтому
# ждём появления ответа, а не угадываем время.
HEALTH=""
for _ in $(seq 1 20); do
  HEALTH=$(curl -s -m 3 "http://127.0.0.1:$PORT/health" 2>/dev/null || true)
  [ -n "$HEALTH" ] && break
  sleep 1
done

# Версия кода — первый вопрос при разборе «почему правки не видно».
if [ -d "$APP_DIR/.git" ]; then
  VERSION=$(git -C "$APP_DIR" log --oneline -1 2>/dev/null)
else
  VERSION="неизвестна — каталог не подключён к репозиторию"
fi

if [ -n "$HEALTH" ]; then
  echo "СЛУЖБА: $STATE | ВЕРСИЯ: $VERSION"
  echo "ЗДОРОВЬЕ: ок | $HEALTH"
  exit 0
fi

echo "СЛУЖБА: $STATE | ВЕРСИЯ: $VERSION"
echo "ЗДОРОВЬЕ: не отвечает на порту $PORT"
echo "Последние строки лога:"
journalctl -u $SERVICE -n 10 --no-pager 2>/dev/null | tail -10
exit 1
