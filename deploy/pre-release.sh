#!/usr/bin/env bash
# Перед выкладкой: копия базы и архив прежнего кода, чтобы можно было откатиться.
# Хранятся последние KEEP копий каждого вида (старые удаляются). Вызывается из deploy/release.ps1:
#   bash pre-release.sh <название-выкладки> [сколько хранить]
set -euo pipefail

NAME="${1:?Нужно название выкладки}"
KEEP="${2:-10}"
APP_DIR=/opt/autobider
OUT_DIR="$APP_DIR/backup"

if ! [[ "$NAME" =~ ^[a-z0-9-]+$ ]]; then
    echo "Название выкладки: только строчная латиница, цифры и дефис." >&2
    exit 2
fi
if [ -e "$OUT_DIR/app-before-$NAME.tar.gz" ] || [ -e "$OUT_DIR/app.db.before-$NAME" ]; then
    echo "Копии с названием «$NAME» уже есть: выберите другое название, чтобы не затереть прежние." >&2
    exit 3
fi
mkdir -p "$OUT_DIR"

PY="$APP_DIR/.venv/bin/python"
[ -x "$PY" ] || PY=python3

# База: средствами SQLite, копия целостная, даже если сервис в этот момент пишет.
"$PY" - "$APP_DIR/data/app.db" "$OUT_DIR/app.db.before-$NAME" <<'PY'
import sqlite3, sys
src = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY

# Код: весь каталог app без кэша Python (около 8 МБ).
tar -czf "$OUT_DIR/app-before-$NAME.tar.gz" --exclude='__pycache__' -C "$APP_DIR" app
chmod 600 "$OUT_DIR/app.db.before-$NAME" "$OUT_DIR/app-before-$NAME.tar.gz"

# Оставляем последние KEEP копий каждого вида. Архивы старых редизайнов (web-before-*) не трогаем.
for pattern in 'app.db.before-*' 'app-before-*.tar.gz'; do
    ls -1t "$OUT_DIR"/$pattern 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f --
done

echo "Копии готовы: app.db.before-$NAME и app-before-$NAME.tar.gz (хранятся последние $KEEP каждого вида)"
