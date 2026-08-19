#!/usr/bin/env bash
# Обновление до свежего кода. Безопасно повторять сколько угодно раз.
# Схему базы накатывать отдельно не нужно — приложение делает это само при старте.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=ai-sales
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

echo "== бэкап базы перед обновлением"
bash deploy/backup.sh >/dev/null 2>&1 || echo "   базы ещё нет, пропускаем"

if [ -d .git ]; then
  echo "== свежий код"
  git fetch --quiet origin

  # Куда равняться. Локальная ветка может называться иначе, чем удалённая:
  # каталог, поднятый через «git init», получает master, а на GitHub main —
  # и деплой уходил искать несуществующий origin/master.
  TARGET=$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)
  if [ -z "$TARGET" ]; then
    for CANDIDATE in "origin/$(git rev-parse --abbrev-ref HEAD)" origin/main origin/master; do
      if git rev-parse --verify --quiet "$CANDIDATE" >/dev/null; then
        TARGET=$CANDIDATE
        break
      fi
    done
  fi
  if [ -z "$TARGET" ]; then
    echo "ОСТАНОВКА: не нашёл ветку на origin. Проверьте: git remote -v && git branch -a"
    exit 1
  fi

  echo "   равняемся на $TARGET"
  git reset --hard --quiet "$TARGET"
  echo "   $(git log --oneline -1)"
else
  # Каталог без .git — код когда-то скопировали, а не склонировали. Тогда
  # обновление кода молча не делалось: служба перезапускалась на прежней версии,
  # и правки «не доезжали». Молчать про это нельзя.
  echo
  echo "ОСТАНОВКА: $APP_DIR не является git-репозиторием, свежий код взять негде."
  echo "Разверните как в README:"
  echo "  git clone https://github.com/iarbor04/ai-sales-agent.git ai-sales"
  echo "или подключите этот каталог к репозиторию, сохранив .env и базу:"
  echo "  git init && git remote add origin https://github.com/iarbor04/ai-sales-agent.git"
  echo "  git fetch origin main && git reset --hard origin/main"
  exit 1
fi

echo "== зависимости"
.venv/bin/pip install -q -r requirements.txt

if ! service_owns_dir; then
  echo
  echo "ОСТАНОВКА: служба $SERVICE обслуживает другой каталог."
  echo "Похоже, вы запустили деплой из копии проекта. Перезапуск тронул бы"
  echo "настоящую установку, поэтому останавливаюсь. Разворачивайте копию"
  echo "отдельной службой через deploy/install.sh."
  exit 1
fi

echo "== перезапуск"
$SUDO systemctl restart $SERVICE

# status.sh сам дождётся ответа службы — угадывать время старта не нужно
bash deploy/status.sh
