#!/usr/bin/env bash
# Вернуть установку в пустое состояние: ни лидов, ни переписок, ни настроек.
#
# Нужно, когда копию своей установки отдают новому клиенту: код чистый, а вот
# база остаётся с чужими диалогами. Настройки каналов и ключи тоже стираются —
# отдавать клиенту чужой токен бота нельзя.
#
# Требует подтверждения словом: случайный запуск не должен снести боевую базу.
#
#   bash deploy/reset.sh            # спросит подтверждение
#   bash deploy/reset.sh --yes      # без вопросов, для скриптов
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Имя службы принадлежит установке, а не скрипту: на машине бывает несколько
# копий, и каждая должна управлять своей. install.sh пишет его в .env.
SERVICE=${SERVICE:-$(grep -E '^SERVICE_NAME=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d ' ')}
SERVICE=${SERVICE:-ai-sales}
[ "$(id -u)" -ne 0 ] && SUDO=sudo || SUDO=""

cd "$APP_DIR"

# Скрипты управляют службой systemd по имени, а не по каталогу. Если запустить
# их из копии проекта, они дёрнут службу настоящей установки — и та встанет
# или перезапустится вместе с чужой базой. Поэтому сверяем, тот ли это каталог.
service_owns_dir() {
  local dir
  dir=$(systemctl show -p WorkingDirectory --value "$SERVICE" 2>/dev/null || true)
  [ -n "$dir" ] && [ "$dir" = "$APP_DIR" ]
}

DB=$(grep -E '^DB_PATH=' .env 2>/dev/null | cut -d= -f2 | tr -d ' ' || true)
DB=${DB:-data.db}
MEDIA=$(grep -E '^MEDIA_DIR=' .env 2>/dev/null | cut -d= -f2 | tr -d ' ' || true)
MEDIA=${MEDIA:-media}

LEADS=0
if [ -f "$DB" ]; then
  LEADS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM leads" 2>/dev/null || echo 0)
  MESSAGES=$(sqlite3 "$DB" "SELECT COUNT(*) FROM messages" 2>/dev/null || echo 0)
  echo "В базе сейчас: лидов $LEADS, сообщений ${MESSAGES:-0}"
else
  echo "База ещё не создана — сбрасывать почти нечего"
fi

if [ "${1:-}" != "--yes" ]; then
  echo
  echo "Будут удалены: база $DB, вложения из $MEDIA, все настройки и токены."
  printf 'Напишите СБРОС, чтобы продолжить: '
  read -r ANSWER
  [ "$ANSWER" = "СБРОС" ] || { echo "Отменено, ничего не тронуто."; exit 1; }
fi

echo "== бэкап на случай передумать"
bash deploy/backup.sh >/dev/null 2>&1 || echo "   базы нет, пропускаем"

if service_owns_dir; then
  echo "== остановка службы"
  $SUDO systemctl stop $SERVICE 2>/dev/null || true
  TOUCH_SERVICE=1
else
  echo "== служба ai-sales обслуживает другой каталог — не трогаю её"
  TOUCH_SERVICE=0
fi

echo "== удаление данных"
rm -f "$DB" "$DB-wal" "$DB-shm"
rm -rf "$MEDIA"
mkdir -p "$MEDIA"

if [ "$TOUCH_SERVICE" = "1" ]; then
  echo "== запуск на чистой базе"
  $SUDO systemctl start $SERVICE 2>/dev/null || true
  bash deploy/status.sh || true
else
  echo "== данные этой копии очищены; служба, если нужна, поднимается install.sh"
fi
echo
echo "ГОТОВО. Установка пустая: откройте панель и пройдите «Мастер запуска»."
echo "Бэкап прежней базы лежит в backups/ — если что, восстановит deploy/restore.sh."
