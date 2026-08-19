#!/usr/bin/env bash
# Одна строка о состоянии. Именно её агент показывает владельцу.
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Имя службы принадлежит установке, а не скрипту: на машине бывает несколько
# копий, и каждая должна управлять своей. install.sh пишет его в .env.
# `|| true` обязателен: под `set -euo pipefail` grep, не нашедший строку,
# валит весь скрипт молча — на установках, сделанных до появления SERVICE_NAME,
# деплой просто выходил с кодом 1 и без единого слова.
SERVICE=${SERVICE:-$(grep -E '^SERVICE_NAME=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d ' ' || true)}
SERVICE=${SERVICE:-ai-sales}
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

# Мало убедиться, что на порту наш /health: на машине бывает вторая установка
# того же продукта, и статус показывал бы её данные. Сверяем владельца порта
# с процессом службы.
MAIN_PID=$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null || true)
PORT_PID=$(ss -tlnpH "sport = :$PORT" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2 || true)
if [ -n "$MAIN_PID" ] && [ "$MAIN_PID" != "0" ] && [ -n "$PORT_PID" ] && [ "$MAIN_PID" != "$PORT_PID" ]; then
  echo "СЛУЖБА: $STATE | ВЕРСИЯ: $VERSION"
  echo "ЗДОРОВЬЕ: порт $PORT занят другим процессом ($PORT_PID), служба слушает не его."
  echo "Скорее всего, на машине вторая установка. Смените PORT в .env."
  exit 1
fi

# На порту может отвечать что угодно — другой сервис на той же машине тоже
# вернёт JSON. Убеждаемся, что это наш /health, иначе «здоровье ок» соврёт.
case "$HEALTH" in
  *'"ok":true'*'"model"'*)
    echo "СЛУЖБА: $STATE | ВЕРСИЯ: $VERSION"
    echo "ЗДОРОВЬЕ: ок | $HEALTH"
    exit 0
    ;;
esac

if [ -n "$HEALTH" ]; then
  echo "СЛУЖБА: $STATE | ВЕРСИЯ: $VERSION"
  echo "ЗДОРОВЬЕ: на порту $PORT отвечает не наша служба: $(echo "$HEALTH" | cut -c1-60)"
  exit 1
fi

echo "СЛУЖБА: $STATE | ВЕРСИЯ: $VERSION"
echo "ЗДОРОВЬЕ: не отвечает на порту $PORT"
echo "Последние строки лога:"
journalctl -u $SERVICE -n 10 --no-pager 2>/dev/null | tail -10
exit 1
