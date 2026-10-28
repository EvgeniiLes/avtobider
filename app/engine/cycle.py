"""Один цикл бидера: оценка объявлений, решения, применение и запись в историю.

Правила описаны в docs/algorithms.md. Движок работает с любой площадкой через
интерфейс Platform и не знает, настоящие данные или пример.
"""
import json
import random
import sqlite3

from app.engine.config import EngineConfig
from app.engine.stats import classify, enough_data, prob_b_better, prob_ok
from app.engine.util import EngineError, day_add, days_between, plural, rub
from app.platforms.base import Platform

CLASS_LABELS = {
    "good": "Окупается",
    "borderline": "Пока неясно",
    "bad": "Не окупается",
    "waiting": "Собираем данные",
}


def target_cpa_micro(biz: sqlite3.Row) -> int:
    """Цена клиента, которую готов платить владелец. Нет её, считаем от среднего чека."""
    if biz["target_cpa_micro"] > 0:
        return biz["target_cpa_micro"]
    if biz["avg_check_micro"] > 0:
        return int(biz["avg_check_micro"] * 0.3)
    raise EngineError("Укажите в описании бизнеса, сколько готовы платить за клиента, или средний чек.", "no_target")


def target_lead_micro(biz: sqlite3.Row) -> int:
    """Допустимая цена одного обращения: цена клиента × доля обращений, ставших клиентами."""
    return int(target_cpa_micro(biz) * biz["client_share"])


def unit_stats(conn: sqlite3.Connection, unit_id: int, start: str, end: str) -> dict:
    row = conn.execute(
        "SELECT COALESCE(SUM(impressions),0) AS impressions, COALESCE(SUM(clicks),0) AS clicks, "
        "COALESCE(SUM(spend_micro),0) AS spend, COALESCE(SUM(leads),0) AS leads, COALESCE(SUM(clients),0) AS clients "
        "FROM stats_daily WHERE unit_id = ? AND day BETWEEN ? AND ?",
        (unit_id, start, end),
    ).fetchone()
    return dict(row)


def log_change(conn, business_id, unit_id, day, actor, action, code, text, before, after, evidence=None) -> int:
    cur = conn.execute(
        "INSERT INTO changes (business_id, unit_id, day, actor, action, reason_code, reason_text, "
        "before_json, after_json, evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            business_id, unit_id, day, actor, action, code, text,
            json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False),
            json.dumps(evidence or {}, ensure_ascii=False),
        ),
    )
    return cur.lastrowid


def _lead_cost_text(spend: int, leads: int) -> str:
    if not leads:
        return "обращений нет"
    return f"{plural(leads, 'обращение', 'обращения', 'обращений')}, одно обходится примерно в {rub(spend / leads)}"


def _pause(conn, platform, biz, unit, today, code, text, evidence=None) -> int:
    platform.set_state(unit["id"], "paused", today)
    return log_change(
        conn, biz["id"], unit["id"], today, "auto", "pause", code, text,
        {"state": "active"}, {"state": "paused"}, evidence,
    )


def _finish_experiments(conn, platform, biz, today, as_of, cfg) -> list[int]:
    made = []
    for ex in conn.execute("SELECT * FROM experiments WHERE business_id = ? AND status = 'running'", (biz["id"],)).fetchall():
        a = conn.execute("SELECT * FROM ad_units WHERE id = ?", (ex["unit_a"],)).fetchone()
        b = conn.execute("SELECT * FROM ad_units WHERE id = ?", (ex["unit_b"],)).fetchone()
        if a["state"] != "active" or b["state"] != "active":
            conn.execute("UPDATE experiments SET status = 'cancelled', ended_day = ? WHERE id = ?", (today, ex["id"]))
            continue
        sa = unit_stats(conn, a["id"], ex["started_day"], as_of)
        sb = unit_stats(conn, b["id"], ex["started_day"], as_of)
        ready = (sa["clicks"] >= cfg.ab_min_clicks and sb["clicks"] >= cfg.ab_min_clicks) or \
            days_between(ex["started_day"], today) >= cfg.ab_max_days
        if not ready:
            continue
        rng = random.Random(f"ab:{ex['id']}:{today}")
        p = prob_b_better(sa["clicks"], sa["leads"], sb["clicks"], sb["leads"], rng, cfg.samples)
        cr = lambda s: (s["leads"] / s["clicks"] * 100) if s["clicks"] else 0.0
        if p >= cfg.ab_confidence:
            verdict, winner, loser = "b", b, a
        elif p <= 1 - cfg.ab_confidence:
            verdict, winner, loser = "a", a, b
        else:
            verdict = "draw"
            cpc = lambda s: (s["spend"] / s["clicks"]) if s["clicks"] else float("inf")
            winner, loser = (b, a) if cpc(sb) < cpc(sa) else (a, b)
        crw, crl = (cr(sb), cr(sa)) if winner["id"] == b["id"] else (cr(sa), cr(sb))
        if verdict == "draw":
            text = (
                f"Сравнение закончено: заметной разницы между «{a['title']}» и «{b['title']}» нет. "
                f"Оставили «{winner['title']}»: клик у него дешевле."
            )
        else:
            text = (
                f"Сравнение закончено. Оставили «{winner['title']}»: из каждых 100 кликов у него {crw:.1f} обращений, "
                f"у «{loser['title']}» только {crl:.1f}. Второе объявление остановили."
            )
        made.append(_pause(
            conn, platform, biz, loser, today, "ab_loser", text,
            {"verdict": verdict, "p_b_better": round(p, 3), "a": sa, "b": sb},
        ))
        conn.execute(
            "UPDATE experiments SET status = 'done', verdict = ?, p_better = ?, ended_day = ? WHERE id = ?",
            (verdict, p, today, ex["id"]),
        )
    return made


def run_cycle(conn: sqlite3.Connection, biz: sqlite3.Row, platform: Platform, today: str, cfg: EngineConfig = EngineConfig()) -> list[int]:
    """Выполняет один цикл и возвращает номера созданных записей истории."""
    lead_target = target_lead_micro(biz)
    cpa = target_cpa_micro(biz)
    if lead_target <= 0:
        raise EngineError("Не получилось рассчитать допустимую цену обращения. Проверьте цену клиента и долю обращений.", "no_target")

    as_of = day_add(today, -cfg.lag_days)
    start = day_add(as_of, -(cfg.window_days - 1))
    budget_total = biz["weekly_budget_micro"]
    made: list[int] = []

    def active_units():
        return conn.execute("SELECT * FROM ad_units WHERE business_id = ? AND state = 'active' ORDER BY id", (biz["id"],)).fetchall()

    units = active_units()
    week_spend = sum(unit_stats(conn, u["id"], day_add(as_of, -6), as_of)["spend"] for u in units)

    # 1. Защита бюджета идёт первой и важнее любых других правил.
    if budget_total > 0 and week_spend >= budget_total:
        for u in units:
            made.append(_pause(
                conn, platform, biz, u, today, "budget_exhausted",
                f"Остановили «{u['title']}»: бюджет недели израсходован ({rub(week_spend)} из {rub(budget_total)}).",
                {"week_spend": week_spend, "weekly_budget": budget_total},
            ))
        conn.commit()
        return made
    block_growth = budget_total > 0 and week_spend >= cfg.block_growth_at * budget_total

    # 2. Завершённые A/B-тесты.
    made += _finish_experiments(conn, platform, biz, today, as_of, cfg)
    units = active_units()
    in_test = {
        uid for row in conn.execute("SELECT unit_a, unit_b FROM experiments WHERE business_id = ? AND status = 'running'", (biz["id"],))
        for uid in (row["unit_a"], row["unit_b"])
    }
    tested_before = {
        row["unit_a"] for row in conn.execute("SELECT unit_a FROM experiments WHERE business_id = ?", (biz["id"],))
    }

    # 3. Оценка каждого объявления.
    free = int((1 - cfg.reserve) * budget_total) - sum(u["budget_micro"] for u in units)
    evals = []
    for u in units:
        s = unit_stats(conn, u["id"], start, as_of)
        if not enough_data(s["clicks"], s["spend"], cpa, cfg):
            cls, p = "waiting", 0.0
        else:
            p = prob_ok(s["clicks"], s["leads"], s["spend"], lead_target, random.Random(f"{u['id']}:{today}"), cfg.samples)
            cls = classify(p, cfg)
            if s["leads"] == 0 and s["spend"] >= 2 * lead_target:
                cls = "bad"  # потратили вдвое больше цены обращения, а обращений нет
        prev = conn.execute(
            "SELECT cls FROM evaluations WHERE unit_id = ? AND day < ? ORDER BY day DESC, id DESC LIMIT 1", (u["id"], today)
        ).fetchone()
        conn.execute(
            "INSERT INTO evaluations (unit_id, day, cls, p_ok, clicks, leads, spend_micro) VALUES (?,?,?,?,?,?,?)",
            (u["id"], today, cls, p, s["clicks"], s["leads"], s["spend"]),
        )
        evals.append({"unit": u, "stats": s, "cls": cls, "p": p, "prev": prev["cls"] if prev else None})

    def cooling(u) -> bool:
        return bool(u["last_changed_day"]) and days_between(u["last_changed_day"], today) < cfg.cooldown_days

    def evidence(e):
        return {"clicks": e["stats"]["clicks"], "leads": e["stats"]["leads"], "spend": e["stats"]["spend"],
                "p_ok": round(e["p"], 3), "target_lead": lead_target}

    # 4. Слабые отключаем: освободившийся бюджет возвращается в общий запас.
    for e in evals:
        u = e["unit"]
        if e["cls"] == "bad" and e["prev"] == "bad" and not cooling(u) and u["id"] not in in_test:
            s = e["stats"]
            made.append(_pause(
                conn, platform, biz, u, today, "bad_unit",
                f"Остановили «{u['title']}»: потратили {rub(s['spend'])}, {_lead_cost_text(s['spend'], s['leads'])}. "
                f"Вы готовы платить за обращение не больше {rub(lead_target)}.",
                evidence(e),
            ))
            free += u["budget_micro"]

    # 5. Хорошим добавляем бюджет, начиная с самых надёжных.
    if not block_growth:
        good = sorted((e for e in evals if e["cls"] == "good" and e["prev"] == "good"), key=lambda e: -e["p"])
        for e in good:
            u = e["unit"]
            if cooling(u) or u["id"] in in_test:
                continue
            new = int(u["budget_micro"] * (1 + cfg.raise_step))
            if new - u["budget_micro"] > free:
                continue
            platform.set_budget(u["id"], new, today)
            free -= new - u["budget_micro"]
            s = e["stats"]
            made.append(log_change(
                conn, biz["id"], u["id"], today, "auto", "set_budget", "good_unit",
                f"Добавили {round(cfg.raise_step * 100)}% бюджета для «{u['title']}»: {_lead_cost_text(s['spend'], s['leads'])}, "
                f"это дешевле вашей цели ({rub(lead_target)}). Бюджет на неделю: {rub(u['budget_micro'])} → {rub(new)}.",
                {"budget_micro": u["budget_micro"]}, {"budget_micro": new}, evidence(e),
            ))

        # 6. Пограничным даём шанс: сравниваем с новым вариантом.
        for e in evals:
            u = e["unit"]
            if e["cls"] != "borderline" or e["prev"] != "borderline" or cooling(u):
                continue
            if u["id"] in in_test or u["id"] in tested_before or u["variant_of"] is not None:
                continue
            if u["budget_micro"] > free:
                continue
            variant_id = platform.create_variant(u["id"], today)
            variant = conn.execute("SELECT title FROM ad_units WHERE id = ?", (variant_id,)).fetchone()
            ex = conn.execute(
                "INSERT INTO experiments (business_id, unit_a, unit_b, started_day) VALUES (?,?,?,?)",
                (biz["id"], u["id"], variant_id, today),
            )
            platform.set_state(u["id"], "active", today)  # объявление не менялось, но тест начался: даём ему «остыть»
            free -= u["budget_micro"]
            s = e["stats"]
            made.append(log_change(
                conn, biz["id"], u["id"], today, "auto", "start_test", "borderline_unit",
                f"Запустили сравнение для «{u['title']}»: пока не ясно, окупается ли оно ({_lead_cost_text(s['spend'], s['leads'])}, "
                f"цель {rub(lead_target)}). Добавили второй вариант «{variant['title']}» с таким же бюджетом.",
                {}, {"variant_unit_id": variant_id, "experiment_id": ex.lastrowid}, evidence(e),
            ))

    conn.commit()
    return made


def rollback(conn: sqlite3.Connection, platform: Platform, biz: sqlite3.Row, change_id: int, today: str, force: bool = False) -> int:
    """Отменяет изменение. Возвращает номер новой записи в истории."""
    ch = conn.execute("SELECT * FROM changes WHERE id = ? AND business_id = ?", (change_id, biz["id"])).fetchone()
    if ch is None:
        raise EngineError("Такое изменение не найдено.", "not_found")
    if ch["reverted_by"]:
        raise EngineError("Это изменение уже отменено.", "already")
    if ch["action"] == "rollback":
        raise EngineError("Отмену отменить нельзя. Если нужно, верните настройку вручную.", "not_allowed")
    before, after = json.loads(ch["before_json"]), json.loads(ch["after_json"])

    if ch["action"] == "start_test":
        platform.set_state(after["variant_unit_id"], "paused", today)
        conn.execute("UPDATE experiments SET status = 'cancelled', ended_day = ? WHERE id = ?", (today, after["experiment_id"]))
        text = "Вы отменили сравнение: второй вариант остановлен, объявление работает как раньше."
        new_before, new_after = after, before
    else:
        unit = conn.execute("SELECT * FROM ad_units WHERE id = ?", (ch["unit_id"],)).fetchone()
        if unit is None:
            raise EngineError("Объявления уже нет, отменять нечего.", "not_found")
        current = {"state": unit["state"], "budget_micro": unit["budget_micro"]}
        if any(current[k] != v for k, v in after.items()) and not force:
            raise EngineError(
                "С тех пор это объявление меняли ещё раз. Если отменить сейчас, вернётся состояние до этого изменения. Подтвердите отмену.",
                "conflict",
            )
        if "state" in before:
            platform.set_state(unit["id"], before["state"], today)
        if "budget_micro" in before:
            platform.set_budget(unit["id"], before["budget_micro"], today)
        text = f"Вы отменили изменение для «{unit['title']}»: вернули как было."
        new_before, new_after = {k: current[k] for k in before}, before

    new_id = log_change(conn, biz["id"], ch["unit_id"], today, "user", "rollback", "user_rollback", text, new_before, new_after,
                        {"original_change_id": change_id})
    conn.execute("UPDATE changes SET reverted_by = ? WHERE id = ?", (new_id, change_id))
    conn.commit()
    return new_id
