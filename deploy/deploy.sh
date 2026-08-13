#!/usr/bin/env bash
# Обновление до свежего кода. Безопасно повторять сколько угодно раз.
# Схему базы накатывать отдельно не нужно — приложение делает это само при старте.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=ai-sales
[ "$(id -u)" -ne 0 ] && SUDO=sudo || SUDO=""

cd "$APP_DIR"

echo "== бэкап базы перед обновлением"
bash deploy/backup.sh >/dev/null 2>&1 || echo "   базы ещё нет, пропускаем"

if [ -d .git ]; then
  echo "== свежий код"
  git fetch --quiet origin
  git reset --hard --quiet "origin/$(git rev-parse --abbrev-ref HEAD)"
fi

echo "== зависимости"
.venv/bin/pip install -q -r requirements.txt

echo "== перезапуск"
$SUDO systemctl restart $SERVICE
sleep 3

bash deploy/status.sh
