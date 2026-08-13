#!/usr/bin/env bash
# Первичная установка на VPS. Запускать один раз, но повтор безопасен.
#
# Занимает несколько минут, поэтому запускать в фоне и следить за логом:
#   setsid nohup bash deploy/install.sh > install.log 2>&1 < /dev/null &
#   tail -5 install.log
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=ai-sales
PYTHON=python3

echo "== установка в $APP_DIR"

if [ "$(id -u)" -ne 0 ]; then
  SUDO=sudo
else
  SUDO=""
fi

echo "== системные пакеты"
export DEBIAN_FRONTEND=noninteractive
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq python3-venv python3-pip sqlite3 nginx >/dev/null

echo "== виртуальное окружение"
[ -d "$APP_DIR/.venv" ] || $PYTHON -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "== конфигурация"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  # генерируем секрет сразу, чтобы он не остался из примера
  SECRET=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  sed -i "s#^SECRET_KEY=.*#SECRET_KEY=$SECRET#" "$APP_DIR/.env"
  echo "   создан .env — впишите токены перед запуском"
else
  echo "   .env уже есть, не трогаем"
fi

echo "== служба systemd"
$SUDO tee /etc/systemd/system/$SERVICE.service >/dev/null <<UNIT
[Unit]
Description=AI Sales Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/run.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

$SUDO systemctl daemon-reload
$SUDO systemctl enable $SERVICE >/dev/null 2>&1 || true
$SUDO systemctl restart $SERVICE

sleep 3
echo "== проверка"
bash "$APP_DIR/deploy/status.sh"

echo
echo "ГОТОВО. Дальше:"
echo "  1. Впишите токены:  nano $APP_DIR/.env"
echo "  2. Перезапустите:   bash $APP_DIR/deploy/deploy.sh"
echo "  3. Публичный адрес: bash $APP_DIR/deploy/nginx.sh ВАШ.ДОМЕН"
