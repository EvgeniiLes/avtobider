"""SQLite: подключение и создание таблиц."""
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from app.config import get_settings

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);

-- Рекламные кабинеты пользователя. Токен хранится только в зашифрованном виде.
CREATE TABLE IF NOT EXISTS ad_accounts (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    platform     TEXT NOT NULL DEFAULT 'yandex_direct',
    token_enc    TEXT NOT NULL,
    client_login TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Описание бизнеса. Деньги в микрорублях (рубли × 1 000 000).
CREATE TABLE IF NOT EXISTS businesses (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    kind                TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    city                TEXT NOT NULL DEFAULT '',
    site_url            TEXT NOT NULL DEFAULT '',
    tone                TEXT NOT NULL DEFAULT '',
    color_main          TEXT NOT NULL DEFAULT '',
    color_extra         TEXT NOT NULL DEFAULT '',
    avg_check_micro     INTEGER NOT NULL DEFAULT 0,
    target_cpa_micro    INTEGER NOT NULL DEFAULT 0,
    weekly_budget_micro INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'draft',
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Файлы бизнеса для создания рекламы: логотип, фото, видео.
CREATE TABLE IF NOT EXISTS business_assets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- номера не переиспользуются: на них ссылаются адреса и кэш браузера
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,                 -- logo, photo, video
    tag         TEXT NOT NULL DEFAULT '',      -- interior, work, team, product, other
    caption     TEXT NOT NULL DEFAULT '',
    stored_name TEXT NOT NULL,
    orig_name   TEXT NOT NULL DEFAULT '',
    mime        TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    width       INTEGER,
    height      INTEGER,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_assets_business ON business_assets (business_id);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Подключение живёт один запрос и не делится между запросами, поэтому его
    # безопасно передавать между потоком зависимости и обработчиком.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """Подключение к базе на время одного запроса (зависимость FastAPI)."""
    conn = connect(get_settings().db_path)
    try:
        yield conn
    finally:
        conn.close()
