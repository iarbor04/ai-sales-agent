#!/usr/bin/env bash
# Откат на предыдущий коммит. База не трогается — она совместима вперёд и назад.
# Если нужно вернуть и данные: deploy/restore.sh backups/db-ДАТА.sqlite
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ "$(id -u)" -ne 0 ] && SUDO=sudo || SUDO=""
cd "$APP_DIR"

echo "== откат кода на один коммит назад"
git reset --hard --quiet HEAD~1
.venv/bin/pip install -q -r requirements.txt
$SUDO systemctl restart ai-sales
sleep 3
bash deploy/status.sh
