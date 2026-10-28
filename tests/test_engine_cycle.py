import json

import pytest

from app.db import connect, init_db
from app.engine.config import EngineConfig
from app.engine.cycle import rollback, run_cycle, target_lead_micro, unit_stats
from app.engine.util import EngineError, MICRO, day_add
from app.platforms.simulator import SimulatedPlatform, advance_day, start_demo, reset_demo

START = "2026-01-01"
BUDGET = 20_000 * MICRO


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "t.db"
    init_db(path)
    c = connect(path)
    c.execute("INSERT INTO users (email, password_hash) VALUES ('a@b.c', 'x')")
    c.execute(
        "INSERT INTO businesses (user_id, name, kind, city, weekly_budget_micro, target_cpa_micro, client_share) "
        "VALUES (1, 'Лотос', 'Маникюр', 'Москва', ?, ?, 0.3)",
        (BUDGET, 800 * MICRO),
    )
    c.commit()
    yield c
    c.close()


def biz(conn):
    return conn.execute("SELECT * FROM businesses WHERE id = 1").fetchone()


def simulate(conn, days, propose=False):
    b = biz(conn)
    platform = SimulatedPlatform(conn)
    start_demo(conn, b, target_lead_micro(b), START)
    all_changes = []
    for _ in range(days):
        today = advance_day(conn, b)
        all_changes += run_cycle(conn, b, platform, today, propose=propose)
        check_invariants(conn, today)
    return platform, all_changes


def today_of(conn):
    return conn.execute("SELECT today FROM sim_state").fetchone()[0]


def check_invariants(conn, today):
    b = biz(conn)
    active_budget = conn.execute(
        "SELECT COALESCE(SUM(budget_micro),0) FROM ad_units WHERE business_id = 1 AND state = 'active'"
    ).fetchone()[0]
    assert active_budget <= 0.9 * b["weekly_budget_micro"] + 1, "раздали больше допустимого"
    week = conn.execute(
        "SELECT COALESCE(SUM(spend_micro),0) FROM stats_daily s JOIN ad_units u ON u.id = s.unit_id "
        "WHERE u.business_id = 1 AND s.day BETWEEN ? AND ?",
        (day_add(today, -6), today),
    ).fetchone()[0]
    assert week <= b["weekly_budget_micro"], "расход недели превысил бюджет"


def true_ratio(conn, unit_id):
    """Во сколько раз обращение дороже цели на самом деле (знает только тест)."""
    s = conn.execute("SELECT * FROM sim_units WHERE unit_id = ?", (unit_id,)).fetchone()
    return s["cpc_micro"] / s["cr"] / target_lead_micro(biz(conn))


# ---------- основной сценарий ----------

def test_full_run(conn):
    simulate(conn, 40)
    paused = conn.execute("SELECT * FROM ad_units WHERE state = 'paused'").fetchall()
    assert paused, "слабые объявления должны быть остановлены"
    for u in paused:
        reason = conn.execute(
            "SELECT reason_code FROM changes WHERE unit_id = ? AND action = 'pause' ORDER BY id DESC LIMIT 1", (u["id"],)
        ).fetchone()
        if reason["reason_code"] == "bad_unit":
            assert true_ratio(conn, u["id"]) > 1.0, "остановили объявление, которое на самом деле окупается"

    raises = conn.execute("SELECT * FROM changes WHERE action = 'set_budget'").fetchall()
    assert raises, "хорошим объявлениям должны добавить бюджет"
    for ch in raises:
        before, after = json.loads(ch["before_json"]), json.loads(ch["after_json"])
        assert after["budget_micro"] == int(before["budget_micro"] * 1.2)
        assert true_ratio(conn, ch["unit_id"]) < 1.3, "подняли бюджет объявлению, которое не окупается"


def test_every_change_has_plain_reason_and_evidence(conn):
    simulate(conn, 40)
    rows = conn.execute("SELECT * FROM changes").fetchall()
    assert rows
    for ch in rows:
        assert ch["reason_text"].strip() and "{" not in ch["reason_text"]
        assert "₽" in ch["reason_text"] or ch["reason_code"] in ("ab_loser", "borderline_unit")
        assert json.loads(ch["evidence_json"]) is not None


def test_plural_forms():
    from app.engine.util import plural
    forms = ("обращение", "обращения", "обращений")
    assert plural(1, *forms) == "1 обращение"
    assert plural(2, *forms) == "2 обращения"
    assert plural(5, *forms) == "5 обращений"
    assert plural(11, *forms) == "11 обращений"
    assert plural(12, *forms) == "12 обращений"
    assert plural(21, *forms) == "21 обращение"
    assert plural(0, *forms) == "0 обращений"


def test_cooldown_between_changes(conn):
    simulate(conn, 40)
    rows = conn.execute(
        "SELECT unit_id, day FROM changes WHERE actor = 'auto' AND action != 'start_test' ORDER BY unit_id, id"
    ).fetchall()
    last = {}
    for r in rows:
        if r["unit_id"] in last:
            gap = (__import__("datetime").date.fromisoformat(r["day"]) - __import__("datetime").date.fromisoformat(last[r["unit_id"]])).days
            assert gap >= 2
        last[r["unit_id"]] = r["day"]


def test_ab_test_runs_and_finishes(conn):
    simulate(conn, 60)
    tests = conn.execute("SELECT * FROM experiments").fetchall()
    assert tests, "для пограничных объявлений должен запуститься тест"
    assert any(t["status"] == "done" and t["verdict"] in ("a", "b", "draw") for t in tests)
    finished = conn.execute("SELECT * FROM experiments WHERE status = 'done'").fetchall()
    for t in finished:
        states = {conn.execute("SELECT state FROM ad_units WHERE id = ?", (t[k],)).fetchone()[0] for k in ("unit_a", "unit_b")}
        assert "paused" in states, "после вердикта один вариант должен быть остановлен"


# ---------- защита бюджета ----------

def test_budget_exhausted_pauses_everything(conn):
    b = biz(conn)
    platform = SimulatedPlatform(conn)
    start_demo(conn, b, target_lead_micro(b), START)
    today = day_add(START, 3)
    unit = conn.execute("SELECT id FROM ad_units LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO stats_daily (unit_id, day, clicks, spend_micro) VALUES (?, ?, 500, ?)",
        (unit, day_add(today, -1), BUDGET + MICRO),
    )
    made = run_cycle(conn, b, platform, today)
    assert len(made) == 6
    assert conn.execute("SELECT COUNT(*) FROM ad_units WHERE state = 'active'").fetchone()[0] == 0
    assert "бюджет недели израсходован" in conn.execute("SELECT reason_text FROM changes LIMIT 1").fetchone()[0]


def test_growth_blocked_near_budget_limit(conn):
    b = biz(conn)
    platform = SimulatedPlatform(conn)
    start_demo(conn, b, target_lead_micro(b), START)
    unit_ids = [r[0] for r in conn.execute("SELECT id FROM ad_units")]
    today = day_add(START, 10)
    # 92% бюджета уже потрачено, и все объявления выглядят отлично: рост всё равно запрещён
    for uid in unit_ids:
        for d in range(1, 11):
            conn.execute(
                "INSERT INTO stats_daily (unit_id, day, clicks, spend_micro, leads) VALUES (?,?,?,?,?)",
                (uid, day_add(START, d), 20, 1550 * MICRO if d >= 8 else 0, 10),
            )
    conn.execute("UPDATE ad_units SET last_changed_day = ?", (START,))
    for uid in unit_ids:
        conn.execute("INSERT INTO evaluations (unit_id, day, cls) VALUES (?, ?, 'good')", (uid, day_add(today, -1)))
    spent = conn.execute(
        "SELECT SUM(spend_micro) FROM stats_daily WHERE day BETWEEN ? AND ?", (day_add(today, -7), day_add(today, -1))
    ).fetchone()[0]
    assert spent >= 0.9 * BUDGET and spent < BUDGET, spent
    run_cycle(conn, b, platform, today)
    assert conn.execute("SELECT COUNT(*) FROM changes WHERE action = 'set_budget'").fetchone()[0] == 0


def test_no_target_gives_plain_error(conn):
    conn.execute("UPDATE businesses SET target_cpa_micro = 0, avg_check_micro = 0")
    conn.commit()
    with pytest.raises(EngineError) as e:
        target_lead_micro(biz(conn))
    assert "Укажите" in e.value.message


def test_target_from_average_check(conn):
    conn.execute("UPDATE businesses SET target_cpa_micro = 0, avg_check_micro = ?", (3000 * MICRO,))
    conn.commit()
    assert target_lead_micro(biz(conn)) == int(3000 * MICRO * 0.3 * 0.3)


# ---------- откат ----------

def first_pause(conn):
    return conn.execute("SELECT * FROM changes WHERE action = 'pause' AND actor = 'auto' ORDER BY id LIMIT 1").fetchone()


def test_rollback_restores_state_and_logs(conn):
    platform, _ = simulate(conn, 40)
    ch = first_pause(conn)
    today = conn.execute("SELECT today FROM sim_state").fetchone()[0]
    new_id = rollback(conn, platform, biz(conn), ch["id"], today)
    assert conn.execute("SELECT state FROM ad_units WHERE id = ?", (ch["unit_id"],)).fetchone()[0] == "active"
    log = conn.execute("SELECT * FROM changes WHERE id = ?", (new_id,)).fetchone()
    assert log["actor"] == "user" and log["action"] == "rollback"
    assert conn.execute("SELECT reverted_by FROM changes WHERE id = ?", (ch["id"],)).fetchone()[0] == new_id
    with pytest.raises(EngineError) as again:
        rollback(conn, platform, biz(conn), ch["id"], today)
    assert again.value.code == "already"
    with pytest.raises(EngineError) as undo:
        rollback(conn, platform, biz(conn), new_id, today)
    assert undo.value.code == "not_allowed"


def test_rollback_conflict_needs_confirmation(conn):
    platform, _ = simulate(conn, 40)
    ch = first_pause(conn)
    today = conn.execute("SELECT today FROM sim_state").fetchone()[0]
    platform.set_state(ch["unit_id"], "active", today)  # после остановки объявление включили вручную
    with pytest.raises(EngineError) as e:
        rollback(conn, platform, biz(conn), ch["id"], today)
    assert e.value.code == "conflict"
    rollback(conn, platform, biz(conn), ch["id"], today, force=True)
    assert conn.execute("SELECT state FROM ad_units WHERE id = ?", (ch["unit_id"],)).fetchone()[0] == "active"


def test_rollback_budget_raise(conn):
    platform, _ = simulate(conn, 40)
    ch = conn.execute("SELECT * FROM changes WHERE action = 'set_budget' ORDER BY id LIMIT 1").fetchone()
    today = conn.execute("SELECT today FROM sim_state").fetchone()[0]
    before = json.loads(ch["before_json"])["budget_micro"]
    rollback(conn, platform, biz(conn), ch["id"], today, force=True)
    assert conn.execute("SELECT budget_micro FROM ad_units WHERE id = ?", (ch["unit_id"],)).fetchone()[0] == before


def test_rollback_of_test_stops_variant(conn):
    platform, _ = simulate(conn, 30)
    ch = conn.execute("SELECT * FROM changes WHERE action = 'start_test' ORDER BY id LIMIT 1").fetchone()
    assert ch is not None
    after = json.loads(ch["after_json"])
    today = conn.execute("SELECT today FROM sim_state").fetchone()[0]
    rollback(conn, platform, biz(conn), ch["id"], today)
    assert conn.execute("SELECT state FROM ad_units WHERE id = ?", (after["variant_unit_id"],)).fetchone()[0] == "paused"
    assert conn.execute("SELECT status FROM experiments WHERE id = ?", (after["experiment_id"],)).fetchone()[0] == "cancelled"


def test_reset_clears_demo(conn):
    simulate(conn, 10)
    reset_demo(conn, 1)
    for table in ("campaigns", "ad_units", "stats_daily", "changes", "experiments", "sim_state", "sim_units", "evaluations"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_demo_needs_budget(conn):
    conn.execute("UPDATE businesses SET weekly_budget_micro = 0")
    conn.commit()
    with pytest.raises(EngineError) as e:
        start_demo(conn, biz(conn), 100 * MICRO, START)
    assert "бюджет" in e.value.message
