#!/usr/bin/env bash
# Первичная установка на VPS. Запускать один раз, но повтор безопасен.
#
# Занимает несколько минут, поэтому запускать в фоне и следить за логом:
#   setsid nohup bash deploy/install.sh > install.log 2>&1 < /dev/null &
#   tail -5 install.log
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Имя службы можно переопределить: на одной машине бывает несколько установок,
# и вторая не должна занимать имя первой.
#   SERVICE=ai-sales-demo bash deploy/install.sh
SERVICE=${SERVICE:-ai-sales}
PYTHON=python3

# Если служба с таким именем уже обслуживает другой каталог, установка молча
# переписала бы её unit, и работающая установка начала бы запускать чужой код.
EXISTING=$(systemctl show -p WorkingDirectory --value "$SERVICE" 2>/dev/null || true)
if [ -n "$EXISTING" ] && [ "$EXISTING" != "$APP_DIR" ]; then
  echo "ОСТАНОВКА: служба $SERVICE уже обслуживает $EXISTING."
  echo "Установка перезаписала бы её и увела работающий сервис на этот каталог."
  echo "Задайте другое имя, если это вторая установка на машине:"
  echo "  SERVICE=$SERVICE-2 bash deploy/install.sh"
  exit 1
fi

echo "== установка в $APP_DIR"

if [ "$(id -u)" -ne 0 ]; then
  SUDO=sudo
else
  SUDO=""
fi

echo "== системные пакеты"
export DEBIAN_FRONTEND=noninteractive
$SUDO apt-get update -qq
# curl нужен самому скрипту: определить внешний IP и дождаться ответа службы.
# nginx ставит nginx.sh — базовой установке домен не нужен, а лишняя служба
# на 80-м порту сбивает с толку.
$SUDO apt-get install -y -qq python3-venv python3-pip sqlite3 curl >/dev/null

echo "== виртуальное окружение"
[ -d "$APP_DIR/.venv" ] || $PYTHON -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "== конфигурация"
[ -f "$APP_DIR/.env" ] || cp "$APP_DIR/.env.example" "$APP_DIR/.env"

# Заполняем то, что раньше приходилось дописывать руками после установки:
# пароль панели, секрет сессий и публичный адрес. Уже заданное не трогаем —
# повторный запуск не должен менять пароль работающей установки.
set_env() {  # ключ значение
  if grep -qE "^$1=" "$APP_DIR/.env"; then
    sed -i "s#^$1=.*#$1=$2#" "$APP_DIR/.env"
  else
    printf '%s=%s\n' "$1" "$2" >> "$APP_DIR/.env"
  fi
}
get_env() { grep -E "^$1=" "$APP_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' '; }

if [ -z "$(get_env SECRET_KEY)" ]; then
  set_env SECRET_KEY "$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi

PASSWORD=$(get_env ADMIN_PASSWORD)
case "$PASSWORD" in
  ""|admin|смените-обязательно)
    # 12 символов без похожих друг на друга: пароль диктуют голосом и в чат
    PASSWORD=$(LC_ALL=C tr -dc 'A-HJ-NP-Za-km-z2-9' < /dev/urandom | head -c 12)
    set_env ADMIN_PASSWORD "$PASSWORD"
    NEW_PASSWORD=1
    ;;
esac

PORT_SET=$(get_env PORT); PORT_SET=${PORT_SET:-8000}
PUBLIC=$(get_env PUBLIC_URL)
case "$PUBLIC" in
  ""|http://localhost*)
    IP=$(curl -4 -s --max-time 5 ifconfig.me || hostname -I | awk '{print $1}')
    PUBLIC="http://${IP:-127.0.0.1}:$PORT_SET"
    set_env PUBLIC_URL "$PUBLIC"
    ;;
esac

# Порт занят другой установкой — вторая копия молча не поднимется, а статус
# покажет здоровье первой. Проверяем до создания службы.
PORT_WANTED=$(grep -E '^PORT=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d ' ' || true)
PORT_WANTED=${PORT_WANTED:-8000}
BUSY_PID=$(ss -tlnpH "sport = :$PORT_WANTED" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2 || true)
if [ -n "$BUSY_PID" ] && ! systemctl show -p MainPID --value "$SERVICE" 2>/dev/null | grep -qx "$BUSY_PID"; then
  BUSY_WHO=$(ps -p "$BUSY_PID" -o comm= 2>/dev/null || echo "процесс $BUSY_PID")
  echo "ОСТАНОВКА: порт $PORT_WANTED уже занят ($BUSY_WHO)."
  echo "Впишите свободный порт в $APP_DIR/.env и запустите установку заново:"
  echo "  sed -i 's/^PORT=.*/PORT=8010/' $APP_DIR/.env"
  exit 1
fi

# Запоминаем имя службы в установке, чтобы остальные скрипты управляли своей.
if grep -qE '^SERVICE_NAME=' "$APP_DIR/.env"; then
  sed -i "s#^SERVICE_NAME=.*#SERVICE_NAME=$SERVICE#" "$APP_DIR/.env"
else
  printf '\n# Имя службы systemd этой установки.\nSERVICE_NAME=%s\n' "$SERVICE" >> "$APP_DIR/.env"
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

echo "== проверка"
# Ждём ответа службы, а не спим наугад: на медленной машине трёх секунд мало,
# и установка объявляла бы поломку на живом сервисе.
for _ in $(seq 1 20); do
  curl -sf "http://127.0.0.1:$PORT_SET/health" >/dev/null 2>&1 && break
  sleep 2
done
bash "$APP_DIR/deploy/status.sh"

echo
echo "================ ГОТОВО ================"
echo "Панель:  $PUBLIC"
echo "Логин:   $(get_env ADMIN_LOGIN)"
if [ -n "${NEW_PASSWORD:-}" ]; then
  echo "Пароль:  $PASSWORD"
else
  echo "Пароль:  прежний, из $APP_DIR/.env"
fi
echo "========================================"
echo
echo "Больше ничего вписывать не нужно: бота и ключ модели владелец добавляет"
echo "в панели, раздел «ИИ-продажник» ведёт по шагам."
echo "Своё доменное имя с HTTPS: bash $APP_DIR/deploy/nginx.sh ВАШ.ДОМЕН"
