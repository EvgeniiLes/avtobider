#!/usr/bin/env bash
# Ретранслятор Telegram Bot API на сервере за границей (например, в Швеции). Запускать на ЭТОМ сервере от root:
#   bash setup-tg-relay.sh tg.addtracker.ru you@example.com [IP нашего сервера, по умолчанию 89.111.175.76]
# До запуска: у адреса (tg.addtracker.ru) должна быть запись A на IP этого сервера, порты 80 и 443 открыты.
# Что делает: ставит nginx и certbot, пропускает к api.telegram.org только три метода и только с IP нашего сервера, выпускает сертификат.
# Скрипт можно запускать повторно.
set -euo pipefail

DOMAIN="${1:?Нужен адрес ретранслятора, например tg.addtracker.ru}"
EMAIL="${2:?Нужна почта для сертификата Let’s Encrypt}"
ALLOW_IP="${3:-89.111.175.76}"

if ! [[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || ! [[ "$ALLOW_IP" =~ ^[0-9.]+$ ]]; then
    echo "Проверьте адрес ретранслятора и IP нашего сервера: допустимы только латиница, цифры, точки и дефис." >&2
    exit 2
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y nginx certbot python3-certbot-nginx

cat > /etc/nginx/conf.d/tg-relay.conf <<EOF
# Ретранслятор Telegram Bot API для Автобидера: пускает только наш сервер и только нужные методы.
server {
    listen 80;
    server_name $DOMAIN;
    resolver 1.1.1.1 8.8.8.8 valid=300s ipv6=off;

    location ~ ^/bot[^/]+/(getMe|getUpdates|sendMessage)\$ {
        allow $ALLOW_IP;
        deny all;
        set \$upstream https://api.telegram.org;
        proxy_pass \$upstream\$request_uri;
        proxy_ssl_server_name on;
        proxy_set_header Host api.telegram.org;
        proxy_read_timeout 30s;
    }
    location / {
        return 403;
    }
}
EOF

# Если на сервере включён ufw, открываем веб-порты.
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    ufw allow 80/tcp
    ufw allow 443/tcp
fi

nginx -t
systemctl enable --now nginx
systemctl reload nginx
certbot --nginx -d "$DOMAIN" -m "$EMAIL" --agree-tos --non-interactive --redirect

echo
echo "Готово. Проверка: curl -s -o /dev/null -w '%{http_code}\n' https://$DOMAIN/   (должно быть 403)"
echo "Дальше на своём компьютере: deploy\\set-telegram-keys.ps1, в вопросе про API base укажите https://$DOMAIN"
