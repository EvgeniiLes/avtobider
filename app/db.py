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

-- Все цели счётчика Метрики: названия, и по дням и объявлениям сколько раз каждая достигнута (по отчёту Директа).
-- Обращением считается одна выбранная цель (businesses.metrika_goal_id), остальные нужны, чтобы видеть путь посетителя.
CREATE TABLE IF NOT EXISTS metrika_goals (
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    goal_id     TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (business_id, goal_id)
);

CREATE TABLE IF NOT EXISTS goal_stats (
    unit_id     INTEGER NOT NULL REFERENCES ad_units(id) ON DELETE CASCADE,
    day         TEXT NOT NULL,
    goal_id     TEXT NOT NULL,
    conversions INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (unit_id, day, goal_id)
);

-- Заявки с форм на сайтах клиентов (приём: app/routes/leads.py). delivered: ушла ли заявка владельцу в Telegram.
CREATE TABLE IF NOT EXISTS leads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    name        TEXT NOT NULL DEFAULT '',
    contact     TEXT NOT NULL DEFAULT '',
    task        TEXT NOT NULL DEFAULT '',
    page        TEXT NOT NULL DEFAULT '',
    ip_hash     TEXT NOT NULL DEFAULT '',
    delivered   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_leads_business ON leads (business_id, id DESC);

-- Что уже отправлено владельцу в Telegram (отчёт недели, тревоги): чтобы не слать одно и то же каждые 30 минут.
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    key         TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (business_id, kind, key)
);

-- Реальные поисковые запросы, по которым показывалась реклама (отчёт Директа за последние 30 дней, обновляется раз в день):
-- по ним сервис находит запросы, которые тратят деньги впустую (в минус-слова), и те, что приводят обращения (в ключевые фразы).
CREATE TABLE IF NOT EXISTS search_queries (
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    query       TEXT NOT NULL,
    impressions INTEGER NOT NULL DEFAULT 0,
    clicks      INTEGER NOT NULL DEFAULT 0,
    spend_micro INTEGER NOT NULL DEFAULT 0,
    leads       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (business_id, query)
);

-- Срезы рекламы по измерениям (устройство, пол, возраст, площадка, час суток…): отчёты Директа и Метрики, копятся по дням,
-- см. app/direct/slices.py. day пустой у измерений, которые Метрика отдаёт итогом за 30 дней (часы и дни недели); в них clicks = визиты.
CREATE TABLE IF NOT EXISTS dimension_stats (
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    dimension   TEXT NOT NULL,
    key         TEXT NOT NULL,
    day         TEXT NOT NULL DEFAULT '',
    impressions INTEGER NOT NULL DEFAULT 0,
    clicks      INTEGER NOT NULL DEFAULT 0,
    spend_micro INTEGER NOT NULL DEFAULT 0,
    leads       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (business_id, dimension, key, day)
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

-- Креативы: тексты, картинки и видео для объявлений. status: draft, approved, rejected, working, failed.
CREATE TABLE IF NOT EXISTS creatives (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,                 -- text, image, video
    title       TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    asset_id    INTEGER,                       -- файл картинки или видео (business_assets)
    prompt      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'draft',
    source      TEXT NOT NULL DEFAULT 'template',  -- template, claude, fal, user
    external_id TEXT NOT NULL DEFAULT '',      -- задание у генератора, пока видео готовится
    error       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_creatives_business ON creatives (business_id, kind);

-- Идеи стратега для A/B-теста: гипотеза, аудитория, угол, текст, фото и оформление баннера.
CREATE TABLE IF NOT EXISTS briefs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    hypothesis  TEXT NOT NULL DEFAULT '',
    audience    TEXT NOT NULL DEFAULT '',
    angle       TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    asset_id    INTEGER,                       -- выбранное фото (business_assets)
    look        INTEGER NOT NULL DEFAULT 0,    -- оформление баннера: номер из PHOTO_LOOKS
    prompt      TEXT NOT NULL DEFAULT '',      -- промпт баннера, его можно править
    status      TEXT NOT NULL DEFAULT 'idea',  -- idea, used
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_briefs_business ON briefs (business_id);

-- Расходы на генерацию (в центах доллара), чтобы не выйти за лимит.
CREATE TABLE IF NOT EXISTS generations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    cents       INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Сколько клиентов пришло из рекламы за день: отмечает владелец (Метрика видит обращения, но не знает, кто купил).
CREATE TABLE IF NOT EXISTS client_marks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    day         TEXT NOT NULL,
    count       INTEGER NOT NULL,
    UNIQUE (business_id, day)
);

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
    ("ad_units", "style", "TEXT NOT NULL DEFAULT ''"),
    ("ad_units", "photo_type", "TEXT NOT NULL DEFAULT ''"),
    ("ad_units", "angle", "TEXT NOT NULL DEFAULT ''"),
    ("businesses", "visual_hint", "TEXT NOT NULL DEFAULT ''"),
    ("businesses", "address", "TEXT NOT NULL DEFAULT ''"),
    ("businesses", "allow_promo", "INTEGER NOT NULL DEFAULT 0"),   # можно ли писать в рекламе про скидки и акции из описания
    ("briefs", "ref_id", "INTEGER"),   # номер референса-вдохновения из app/refs (NULL: без референса)
    ("creatives", "picked_at", "TEXT"),   # когда владелец вернул этот баннер на объявление: самый свежий по этой дате и показывается, копий не делаем
    ("briefs", "no_photo", "INTEGER NOT NULL DEFAULT 0"),   # 1: баннер без готового фото, ИИ рисует всю сцену с нуля
    ("business_assets", "ai_fx", "REAL"),   # где главный объект на фото (доли ширины и высоты), по нему шаблоны обрезают кадр; NULL: ещё не смотрели
    ("business_assets", "ai_fy", "REAL"),
    ("creatives", "angle", "TEXT NOT NULL DEFAULT ''"),
    ("businesses", "ads_live", "INTEGER"),   # сколько объявлений идёт в рекламу, как выбрал владелец; NULL: столько, сколько позволяет бюджет
    ("creatives", "position", "INTEGER"),   # место текста в списке объявлений, которое владелец выбрал перетаскиванием; NULL: порядок по умолчанию
    ("business_assets", "ai_type", "TEXT NOT NULL DEFAULT ''"),
    ("business_assets", "ai_score", "INTEGER NOT NULL DEFAULT 0"),
    ("business_assets", "ai_note", "TEXT NOT NULL DEFAULT ''"),
    ("business_assets", "ai_brand", "TEXT NOT NULL DEFAULT ''"),   # название бизнеса, с которым ИИ проверял файл: забракованное при другом названии проверяется заново
    ("changes", "status", "TEXT NOT NULL DEFAULT 'applied'"),
    # Директ: одна кампания на весь бизнес, объявления-варианты лежат внутри неё и делят один недельный лимит
    ("businesses", "lead_key", "TEXT NOT NULL DEFAULT ''"),           # секретная часть адреса приёма заявок с сайта
    ("users", "telegram_chat", "TEXT NOT NULL DEFAULT ''"),          # чат владельца с ботом, куда идут отчёты и тревоги
    ("users", "telegram_code", "TEXT NOT NULL DEFAULT ''"),           # одноразовый код привязки, пока владелец не нажал «Start» в боте
    ("users", "telegram_code_at", "TEXT NOT NULL DEFAULT ''"),
    ("users", "notify_on", "INTEGER NOT NULL DEFAULT 1"),             # 0: отчёты и тревоги выключены
    ("businesses", "counter_health", "TEXT NOT NULL DEFAULT ''"),    # результат последней проверки счётчика Метрики (JSON), см. app/direct/counter_health.py
    ("businesses", "queries_day", "TEXT NOT NULL DEFAULT ''"),       # день, когда последний раз загружали поисковые запросы
    ("businesses", "slices_day", "TEXT NOT NULL DEFAULT ''"),        # день, когда последний раз загружали срезы (app/direct/slices.py)
    ("businesses", "slices_status", "TEXT NOT NULL DEFAULT ''"),     # что вышло и что нет по каждому измерению (JSON)
    ("businesses", "audience", "TEXT NOT NULL DEFAULT ''"),        # профиль целевой аудитории (JSON), см. app/direct/audience.py
    ("businesses", "nationwide", "INTEGER NOT NULL DEFAULT 0"),      # 1: показывать рекламу по всей России, города нет (онлайн-услуга)
    ("campaigns", "external_id", "INTEGER"),                          # номер кампании в Директе
    ("campaigns", "weekly_micro", "INTEGER NOT NULL DEFAULT 0"),      # недельный лимит кампании
    ("campaigns", "strategy", "TEXT NOT NULL DEFAULT 'clicks'"),      # clicks: «Максимум кликов», conversions: «Максимум конверсий»
    ("campaigns", "state", "TEXT NOT NULL DEFAULT 'moderation'"),     # состояние кампании целиком по данным Директа
    ("campaigns", "last_changed_day", "TEXT"),                        # когда сервис последний раз менял бюджет или стратегию
    ("campaigns", "created_day", "TEXT"),
    ("changes", "campaign_id", "INTEGER"),                            # изменение касается всей кампании, а не одного объявления
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
