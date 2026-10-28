"""SQLite: подключение и создание таблиц."""
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from app.config import get_settings

SCHEMA_VERSION = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,                -- пусто у тех, кто входит только через Яндекс ID
    yandex_id     TEXT,
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
    maps_url            TEXT NOT NULL DEFAULT '',
    tone               TEXT NOT NULL DEFAULT '',
    color_main          TEXT NOT NULL DEFAULT '',
    color_extra         TEXT NOT NULL DEFAULT '',
    avg_check_micro     INTEGER NOT NULL DEFAULT 0,
    target_cpa_micro    INTEGER NOT NULL DEFAULT 0,
    weekly_budget_micro INTEGER NOT NULL DEFAULT 0,
    client_share        REAL NOT NULL DEFAULT 0.3,  -- какая доля обращений становится клиентами
    mode                TEXT NOT NULL DEFAULT 'auto', -- ask: сначала спрашивать владельца, auto: делать самому
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

-- Кампании и объявления на площадках. source: simulator (пример данных) или площадка.
CREATE TABLE IF NOT EXISTS campaigns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'simulator'
);

CREATE TABLE IF NOT EXISTS ad_units (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    business_id     INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL DEFAULT 'active',   -- active, paused
    budget_micro    INTEGER NOT NULL DEFAULT 0,       -- бюджет на неделю
    variant_of      INTEGER REFERENCES ad_units(id),
    last_changed_day TEXT,
    created_day     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_units_business ON ad_units (business_id);

CREATE TABLE IF NOT EXISTS stats_daily (
    unit_id     INTEGER NOT NULL REFERENCES ad_units(id) ON DELETE CASCADE,
    day         TEXT NOT NULL,
    impressions INTEGER NOT NULL DEFAULT 0,
    clicks      INTEGER NOT NULL DEFAULT 0,
    spend_micro INTEGER NOT NULL DEFAULT 0,
    leads       INTEGER NOT NULL DEFAULT 0,
    clients     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (unit_id, day)
);

-- Оценка объявления в каждом цикле (нужна для подтверждения решения двумя оценками подряд).
CREATE TABLE IF NOT EXISTS evaluations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id     INTEGER NOT NULL REFERENCES ad_units(id) ON DELETE CASCADE,
    day         TEXT NOT NULL,
    cls         TEXT NOT NULL,       -- good, borderline, bad, waiting
    p_ok        REAL NOT NULL DEFAULT 0,
    clicks      INTEGER NOT NULL DEFAULT 0,
    leads       INTEGER NOT NULL DEFAULT 0,
    spend_micro INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_eval_unit ON evaluations (unit_id, day);

CREATE TABLE IF NOT EXISTS experiments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    unit_a      INTEGER NOT NULL REFERENCES ad_units(id) ON DELETE CASCADE,
    unit_b      INTEGER NOT NULL REFERENCES ad_units(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'running',  -- running, done, cancelled
    verdict     TEXT,                              -- a, b, draw
    p_better    REAL,
    started_day TEXT NOT NULL,
    ended_day   TEXT
);

-- История изменений: что сделал сервис (или вы), почему, как было и как стало.
CREATE TABLE IF NOT EXISTS changes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id   INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    unit_id       INTEGER REFERENCES ad_units(id) ON DELETE SET NULL,
    day           TEXT NOT NULL,
    actor         TEXT NOT NULL,       -- auto, user
    action        TEXT NOT NULL,       -- pause, resume, set_budget, start_test, rollback
    reason_code   TEXT NOT NULL,
    reason_text   TEXT NOT NULL,
    before_json   TEXT NOT NULL,
    after_json    TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'applied',  -- applied, proposed (ждёт решения), rejected
    reverted_by   INTEGER REFERENCES changes(id),
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_changes_business ON changes (business_id, id DESC);

-- Ожидающие подтверждения подключения кабинета (вход по коду).
CREATE TABLE IF NOT EXISTS oauth_pending (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_code TEXT NOT NULL,
    user_code   TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

-- Примерные данные: «текущий день» симуляции и скрытые свойства объявлений.
CREATE TABLE IF NOT EXISTS sim_state (
    business_id INTEGER PRIMARY KEY REFERENCES businesses(id) ON DELETE CASCADE,
    today       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sim_units (
    unit_id   INTEGER PRIMARY KEY REFERENCES ad_units(id) ON DELETE CASCADE,
    cpc_micro INTEGER NOT NULL,
    cr        REAL NOT NULL
);
"""

# Колонки, добавленные после первых версий схемы (для баз, созданных раньше).
ADDED_COLUMNS = [
    ("businesses", "client_share", "REAL NOT NULL DEFAULT 0.3"),
    ("users", "yandex_id", "TEXT"),
    ("businesses", "maps_url", "TEXT NOT NULL DEFAULT ''"),
    ("businesses", "mode", "TEXT NOT NULL DEFAULT 'auto'"),
    ("ad_accounts", "expires_at", "TEXT NOT NULL DEFAULT ''"),
    ("ad_accounts", "refresh_token_enc", "TEXT NOT NULL DEFAULT ''"),
    ("businesses", "account_id", "INTEGER"),
    ("businesses", "marking_confirmed", "INTEGER NOT NULL DEFAULT 0"),
    ("businesses", "metrika_counter_id", "TEXT NOT NULL DEFAULT ''"),
    ("businesses", "metrika_goal_id", "TEXT NOT NULL DEFAULT ''"),
    ("businesses", "last_sync_at", "TEXT NOT NULL DEFAULT ''"),
    ("businesses", "last_sync_error", "TEXT NOT NULL DEFAULT ''"),
    ("campaigns", "account_id", "INTEGER"),
    ("ad_units", "external_campaign_id", "INTEGER"),
    ("ad_units", "external_group_id", "INTEGER"),
    ("ad_units", "external_ad_id", "TEXT"),
    ("ad_units", "status_note", "TEXT NOT NULL DEFAULT ''"),
    ("changes", "status", "TEXT NOT NULL DEFAULT 'applied'"),
]


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, definition in ADDED_COLUMNS:
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_yandex ON users (yandex_id) WHERE yandex_id IS NOT NULL")


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
        _add_missing_columns(conn)
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
