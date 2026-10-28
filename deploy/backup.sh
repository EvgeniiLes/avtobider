#!/usr/bin/env bash
# Ежедневная копия базы и загруженных файлов. Хранится 14 дней в /var/backups/autobider.
# Запускается службой autobider-backup.timer; вручную: sudo /opt/autobider/deploy/backup.sh
set -euo pipefail

APP_DIR=/opt/autobider
OUT_DIR=/var/backups/autobider
STAMP=$(date +%Y-%m-%d_%H%M)
mkdir -p "$OUT_DIR"

# Базу копируем средствами SQLite: копия целостная, даже если сервис в этот момент пишет.
"$APP_DIR/.venv/bin/python" - "$APP_DIR/data/app.db" "$OUT_DIR/app-$STAMP.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY
gzip -f "$OUT_DIR/app-$STAMP.db"

# Загруженные файлы (логотипы, фото, видео) и ключ шифрования: без secret.key сохранённые токены не расшифровать.
tar -czf "$OUT_DIR/files-$STAMP.tar.gz" -C "$APP_DIR/data" --ignore-failed-read uploads secret.key 2>/dev/null || true
chmod 600 "$OUT_DIR"/*

find "$OUT_DIR" -type f -mtime +14 -delete
echo "Копия готова: $OUT_DIR (app-$STAMP.db.gz, files-$STAMP.tar.gz)"
