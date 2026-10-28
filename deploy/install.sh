#!/usr/bin/env bash
# Установка Автобидера на чистый Ubuntu/Debian. Запускать от root: ./install.sh ваш-домен.ru
# Код проекта уже должен лежать в /opt/autobider (см. docs/deployment.md).
set -euo pipefail

DOMAIN="${1:-}"
APP_DIR=/opt/autobider
if [ -z "$DOMAIN" ]; then
  echo "Укажите домен: ./install.sh ваш-домен.ru" >&2
  exit 1
fi
if [ ! -f "$APP_DIR/app/main.py" ]; then
  echo "Не нашёл код в $APP_DIR. Сначала скопируйте проект туда." >&2
  exit 1
fi

echo "==> Ставлю системные пакеты"
apt-get update -y
apt-get install -y python3 python3-venv nginx certbot python3-certbot-nginx fonts-dejavu-core

PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 10) else 0)')
if [ "$PY_OK" != "1" ]; then
  echo "Нужен Python 3.10 или новее." >&2
  exit 1
fi

echo "==> Создаю пользователя и папки"
id autobider >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin autobider
mkdir -p "$APP_DIR/data"

echo "==> Ставлю зависимости Python"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "==> Создал $APP_DIR/.env из шаблона. Впишите в него ключи и перезапустите службу."
fi
chown -R autobider:autobider "$APP_DIR/data"
chown root:autobider "$APP_DIR/.env"
chmod 640 "$APP_DIR/.env"

echo "==> Включаю службу"
cp "$APP_DIR/deploy/autobider.service" /etc/systemd/system/autobider.service
cp "$APP_DIR/deploy/autobider-worker.service" /etc/systemd/system/autobider-worker.service
cp "$APP_DIR/deploy/autobider-backup.service" "$APP_DIR/deploy/autobider-backup.timer" /etc/systemd/system/
chmod +x "$APP_DIR/deploy/backup.sh"
systemctl daemon-reload
systemctl enable --now autobider autobider-worker autobider-backup.timer

echo "==> Настраиваю nginx для $DOMAIN"
sed "s/__DOMAIN__/$DOMAIN/g" "$APP_DIR/deploy/nginx.conf" > /etc/nginx/sites-available/autobider
ln -sf /etc/nginx/sites-available/autobider /etc/nginx/sites-enabled/autobider
nginx -t
systemctl reload nginx

cat <<EOF

Готово. Дальше:
  1) Проверьте, что домен $DOMAIN указывает на этот сервер (запись A).
  2) Получите HTTPS-сертификат:  certbot --nginx -d $DOMAIN
  3) Заполните $APP_DIR/.env и выполните:  systemctl restart autobider
  4) Проверка:  curl https://$DOMAIN/api/health
EOF
