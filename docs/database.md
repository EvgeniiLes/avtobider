# База данных

Нужна ли база? Да, и она уже есть: **SQLite** (это обычная SQL-база в одном файле, `data/app.db`). Для задания её достаточно: один сервер, один процесс сайта и один процесс бидера, режим WAL позволяет им работать одновременно. Если сервис вырастет до множества пользователей, схему можно перенести в PostgreSQL почти без изменений: запросы простые, SQL стандартный.

Правила:
- Деньги хранятся целыми числами в «микрорублях» (рубли × 1 000 000), как в API Директа. Ошибок округления нет.
- Даты и время в UTC, строкой ISO.
- Токены кабинетов только в зашифрованном виде.
- Каждое действие сервиса записывается в историю, ничего не удаляется бесследно.

## Уже есть
`users`, `sessions`, `ad_accounts`, `meta` (см. `app/db.py`).

## Уже добавлено (схема версии 3)
`businesses` (описание бизнеса, стиль, деньги) и `business_assets` (логотип, фото, видео; сами файлы лежат в `data/uploads/<id бизнеса>/`). Номера записей в этих таблицах не переиспользуются (`AUTOINCREMENT`), чтобы старые адреса и кэш браузера не показывали чужие файлы. Ниже в SQL остальные таблицы; их определение для `businesses` в черновике отличается от реального, актуальное лежит в `app/db.py`.

## Нужно добавить

```sql
-- Описание бизнеса и цели
CREATE TABLE businesses (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL,              -- «маникюр», «йога» и т.п.
    description    TEXT NOT NULL,
    city           TEXT NOT NULL DEFAULT '',
    site_url       TEXT NOT NULL DEFAULT '',
    avg_check_micro INTEGER NOT NULL DEFAULT 0,
    client_share   REAL NOT NULL DEFAULT 0.3,  -- какая доля обращений становится клиентами
    target_cpa_micro INTEGER NOT NULL DEFAULT 0, -- цена клиента, которую готовы платить
    weekly_budget_micro INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'draft', -- draft, running, paused
    settings_json  TEXT NOT NULL DEFAULT '{}',  -- пороги правил
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Какие кабинеты ведут этот бизнес
CREATE TABLE business_accounts (
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    account_id  INTEGER NOT NULL REFERENCES ad_accounts(id) ON DELETE CASCADE,
    PRIMARY KEY (business_id, account_id)
);

-- Счётчик Метрики и цель «обращение»
CREATE TABLE metrika_links (
    business_id INTEGER PRIMARY KEY REFERENCES businesses(id) ON DELETE CASCADE,
    counter_id  TEXT NOT NULL,
    goal_id     TEXT NOT NULL,
    token_enc   TEXT NOT NULL
);

-- Кампании на площадках
CREATE TABLE campaigns (
    id            INTEGER PRIMARY KEY,
    business_id   INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    account_id    INTEGER NOT NULL REFERENCES ad_accounts(id) ON DELETE CASCADE,
    external_id   TEXT NOT NULL,               -- id на площадке
    name          TEXT NOT NULL,
    state         TEXT NOT NULL,               -- active, paused, ended
    weekly_limit_micro INTEGER NOT NULL DEFAULT 0,
    managed       INTEGER NOT NULL DEFAULT 1,  -- 0 = чужая кампания, только смотрим
    UNIQUE (account_id, external_id)
);

-- Единицы оценки: объявление или группа
CREATE TABLE ad_units (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    external_id  TEXT NOT NULL,
    kind         TEXT NOT NULL,                -- ad, group
    title        TEXT NOT NULL DEFAULT '',
    body         TEXT NOT NULL DEFAULT '',
    state        TEXT NOT NULL,                -- active, paused, moderation, rejected
    budget_micro INTEGER NOT NULL DEFAULT 0,
    last_changed_at TEXT,                      -- для «остывания»
    UNIQUE (campaign_id, external_id)
);

-- Статистика по дням. source = api или simulator (пример данных)
CREATE TABLE stats_daily (
    unit_id      INTEGER NOT NULL REFERENCES ad_units(id) ON DELETE CASCADE,
    day          TEXT NOT NULL,                -- YYYY-MM-DD
    source       TEXT NOT NULL DEFAULT 'api',
    impressions  INTEGER NOT NULL DEFAULT 0,
    clicks       INTEGER NOT NULL DEFAULT 0,
    spend_micro  INTEGER NOT NULL DEFAULT 0,
    leads        INTEGER NOT NULL DEFAULT 0,   -- обращения из Метрики
    clients      INTEGER NOT NULL DEFAULT 0,   -- отмечены владельцем
    PRIMARY KEY (unit_id, day, source)
);

-- A/B-тесты
CREATE TABLE experiments (
    id          INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    unit_a      INTEGER NOT NULL REFERENCES ad_units(id),
    unit_b      INTEGER NOT NULL REFERENCES ad_units(id),
    status      TEXT NOT NULL DEFAULT 'running', -- running, done
    verdict     TEXT,                          -- a, b, draw
    p_better    REAL,
    started_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at    TEXT
);

-- История изменений: главный журнал
CREATE TABLE changes (
    id            INTEGER PRIMARY KEY,
    business_id   INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    unit_id       INTEGER REFERENCES ad_units(id),
    actor         TEXT NOT NULL,               -- auto, user
    action        TEXT NOT NULL,               -- pause, resume, raise_budget, lower_budget, start_test, finish_test, create, rollback
    reason_code   TEXT NOT NULL,
    reason_text   TEXT NOT NULL,               -- простыми словами
    before_json   TEXT NOT NULL,
    after_json    TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',  -- цифры, на которых принято решение
    applied       INTEGER NOT NULL DEFAULT 0,  -- ушло ли на площадку
    reverted_by   INTEGER REFERENCES changes(id),
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Журнал запусков цикла бидера
CREATE TABLE cycle_runs (
    id          INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,                 -- ok, error, skipped
    error       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_stats_day     ON stats_daily (day);
CREATE INDEX idx_changes_biz   ON changes (business_id, created_at DESC);
CREATE INDEX idx_units_campaign ON ad_units (campaign_id);
```

## Что куда идёт
| Экран | Таблицы |
|---|---|
| Описание бизнеса и бюджет | `businesses`, `business_accounts`, `metrika_links` |
| Статистика (расход, обращения, клиенты, цена клиента) | `stats_daily`, `ad_units`, `campaigns` |
| История с откатом | `changes` |
| Настройки простым языком | `businesses.settings_json` и поля `target_cpa_micro`, `weekly_budget_micro` |
| Симулятор | `stats_daily` с `source = 'simulator'`, на экране помечается «пример данных» |

Добавлять таблицы будем по шагам, вместе с кодом, который их использует, миграциями в `app/db.py` (номер версии схемы лежит в `meta`).
