"""Режим «сначала спрашивать», подтверждение и отказ, стоп-кран."""
import pytest

from app.engine.cycle import approve, proposal_text, reject, resume_all, rollback, stop_all
from app.engine.util import EngineError
from tests.test_engine_cycle import biz, conn, simulate, today_of  # noqa: F401 (фикстура conn)


def proposals(conn, action=None):
    sql = "SELECT * FROM changes WHERE status = 'proposed'" + (" AND action = ?" if action else "") + " ORDER BY id"
    return conn.execute(sql, (action,) if action else ()).fetchall()


def test_proposal_text_reads_as_a_proposal():
    assert proposal_text("Остановили «X»: потратили 1 ₽.") == "Предлагаем остановить «X»: потратили 1 ₽."
    assert proposal_text("Добавили 20% бюджета для «X»") == "Предлагаем добавить 20% бюджета для «X»"
    assert proposal_text("Запустили сравнение для «X». Добавили второй вариант с таким же бюджетом.") == \
        "Предлагаем запустить сравнение для «X». Добавим второй вариант с таким же бюджетом."
    assert proposal_text("Сравнение закончено. Оставили «A». Второе объявление остановили.") == \
        "Сравнение закончено. Предлагаем оставить «A». Второе объявление остановим."


def test_ask_mode_changes_nothing_until_approved(conn):
    simulate(conn, 40, propose=True)
    assert conn.execute("SELECT COUNT(*) FROM ad_units WHERE state = 'paused'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM ad_units WHERE budget_micro != (SELECT MIN(budget_micro) FROM ad_units)").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 0
    open_ = proposals(conn)
    assert open_, "должны быть предложения"
    seen = set()
    for ch in open_:  # одно предложение на действие и объявление
        key = (ch["unit_id"], ch["action"])
        assert key not in seen
        seen.add(key)
        assert ch["reason_text"].startswith("Предлагаем")


def test_approve_pause_applies_and_restores_past_tense(conn):
    platform, _ = simulate(conn, 40, propose=True)
    ch = proposals(conn, "pause")[0]
    approve(conn, platform, biz(conn), ch["id"], today_of(conn))
    row = conn.execute("SELECT * FROM changes WHERE id = ?", (ch["id"],)).fetchone()
    assert row["status"] == "applied" and row["reason_text"].startswith("Остановили")
    assert conn.execute("SELECT state FROM ad_units WHERE id = ?", (ch["unit_id"],)).fetchone()[0] == "paused"
    with pytest.raises(EngineError) as again:
        approve(conn, platform, biz(conn), ch["id"], today_of(conn))
    assert again.value.code == "already"
    rollback(conn, platform, biz(conn), ch["id"], today_of(conn))  # выполненное можно откатить
    assert conn.execute("SELECT state FROM ad_units WHERE id = ?", (ch["unit_id"],)).fetchone()[0] == "active"


def test_approve_budget_raise(conn):
    platform, _ = simulate(conn, 40, propose=True)
    ch = proposals(conn, "set_budget")[0]
    import json
    after = json.loads(ch["after_json"])["budget_micro"]
    approve(conn, platform, biz(conn), ch["id"], today_of(conn))
    assert conn.execute("SELECT budget_micro FROM ad_units WHERE id = ?", (ch["unit_id"],)).fetchone()[0] == after


def test_approve_start_test_creates_variant(conn):
    platform, _ = simulate(conn, 30, propose=True)
    ch = proposals(conn, "start_test")[0]
    approve(conn, platform, biz(conn), ch["id"], today_of(conn))
    ex = conn.execute("SELECT * FROM experiments").fetchall()
    assert len(ex) == 1 and ex[0]["unit_a"] == ch["unit_id"]
    row = conn.execute("SELECT * FROM changes WHERE id = ?", (ch["id"],)).fetchone()
    assert row["status"] == "applied" and "второй вариант «" in row["reason_text"]


def test_reject_keeps_everything_as_is(conn):
    platform, _ = simulate(conn, 40, propose=True)
    ch = proposals(conn, "pause")[0]
    reject(conn, biz(conn), ch["id"], today_of(conn))
    assert conn.execute("SELECT status FROM changes WHERE id = ?", (ch["id"],)).fetchone()[0] == "rejected"
    assert conn.execute("SELECT state FROM ad_units WHERE id = ?", (ch["unit_id"],)).fetchone()[0] == "active"
    with pytest.raises(EngineError):
        rollback(conn, platform, biz(conn), ch["id"], today_of(conn))


def test_stale_proposal_is_closed_not_applied(conn):
    platform, _ = simulate(conn, 40, propose=True)
    ch = proposals(conn, "pause")[0]
    platform.set_state(ch["unit_id"], "paused", today_of(conn))  # владелец остановил сам
    with pytest.raises(EngineError) as e:
        approve(conn, platform, biz(conn), ch["id"], today_of(conn))
    assert e.value.code == "stale"
    assert conn.execute("SELECT status FROM changes WHERE id = ?", (ch["id"],)).fetchone()[0] == "rejected"


def test_budget_protection_ignores_ask_mode(conn):
    """Исчерпанный бюджет останавливает рекламу сразу, не дожидаясь подтверждения."""
    from app.engine.cycle import run_cycle
    from app.engine.util import MICRO, day_add
    from app.platforms.simulator import SimulatedPlatform, start_demo
    from tests.test_engine_cycle import BUDGET, START
    b = biz(conn)
    start_demo(conn, b, 240 * MICRO, START)
    unit = conn.execute("SELECT id FROM ad_units LIMIT 1").fetchone()[0]
    today = day_add(START, 3)
    conn.execute("INSERT INTO stats_daily (unit_id, day, clicks, spend_micro) VALUES (?,?,500,?)", (unit, day_add(today, -1), BUDGET + MICRO))
    run_cycle(conn, b, SimulatedPlatform(conn), today, propose=True)
    assert conn.execute("SELECT COUNT(*) FROM ad_units WHERE state = 'active'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM changes WHERE status = 'proposed'").fetchone()[0] == 0


def test_stop_all_and_resume_all(conn):
    platform, _ = simulate(conn, 5, propose=True)
    # сначала что-то остановим вручную до стоп-крана: оно не должно включиться обратно
    first = conn.execute("SELECT id FROM ad_units ORDER BY id LIMIT 1").fetchone()[0]
    platform.set_state(first, "paused", today_of(conn))
    open_before = len(proposals(conn))
    n = stop_all(conn, platform, biz(conn), today_of(conn))
    assert n == 5
    assert conn.execute("SELECT COUNT(*) FROM ad_units WHERE state = 'active'").fetchone()[0] == 0
    assert len(proposals(conn)) == 0 and open_before >= 0
    back = resume_all(conn, platform, biz(conn), today_of(conn))
    assert back == 5
    active = {r[0] for r in conn.execute("SELECT id FROM ad_units WHERE state = 'active'")}
    assert first not in active and len(active) == 5
    assert resume_all(conn, platform, biz(conn), today_of(conn)) == 0
