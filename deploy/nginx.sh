#!/usr/bin/env bash
# Публичный HTTPS-адрес: nginx перед приложением + сертификат Let's Encrypt.
#
#   bash deploy/nginx.sh sales.example.com
#
# Домен должен уже указывать A-записью на этот сервер, иначе certbot не выдаст
# сертификат. Без домена приложение доступно по http://IP:8000 — этого хватает
# для Telegram в режиме polling, но НЕ хватает для WhatsApp: Cloud API требует HTTPS.
set -euo pipefail

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
  echo "укажите домен: bash deploy/nginx.sh sales.example.com"
  exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ "$(id -u)" -ne 0 ] && SUDO=sudo || SUDO=""
PORT=$(grep -E '^PORT=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d ' ' || true)
PORT=${PORT:-8000}

# nginx нужен только здесь, поэтому и ставим его здесь: базовая установка
# обходится без него и не занимает 80-й порт.
if ! command -v nginx >/dev/null; then
  echo "== ставим nginx"
  export DEBIAN_FRONTEND=noninteractive
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq nginx >/dev/null
fi

# Каталог появляется только у полной сборки nginx — на минимальных образах
# его нет, и ln падал с «No such file or directory».
$SUDO mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled

$SUDO tee /etc/nginx/sites-available/ai-sales >/dev/null <<CONF
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 20M;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
CONF

$SUDO ln -sf /etc/nginx/sites-available/ai-sales /etc/nginx/sites-enabled/ai-sales
$SUDO rm -f /etc/nginx/sites-enabled/default
$SUDO nginx -t
$SUDO systemctl reload nginx

echo "== сертификат"
$SUDO apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
$SUDO certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
     --register-unsafely-without-email --redirect || {
  echo "certbot не смог выдать сертификат — проверьте, что домен указывает на этот сервер"
  exit 1
}

# теперь у приложения есть публичный HTTPS — включаем вебхуки
sed -i "s#^PUBLIC_URL=.*#PUBLIC_URL=https://$DOMAIN#" "$APP_DIR/.env"
sed -i "s#^MODE=.*#MODE=webhook#" "$APP_DIR/.env"
$SUDO systemctl restart ai-sales
sleep 3

echo
echo "ГОТОВО: https://$DOMAIN"
echo "Вебхук WhatsApp в кабинете Meta: https://$DOMAIN/hook/whatsapp"
bash "$APP_DIR/deploy/status.sh"
