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


def log_change(conn, business_id, unit_id, day, actor, action, code, text, before, after, evidence=None, status="applied") -> int:
    cur = conn.execute(
        "INSERT INTO changes (business_id, unit_id, day, actor, action, reason_code, reason_text, "
        "before_json, after_json, evidence_json, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            business_id, unit_id, day, actor, action, code, text,
            json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False),
            json.dumps(evidence or {}, ensure_ascii=False), status,
        ),
    )
    return cur.lastrowid


def has_open_proposal(conn, unit_id: int, action: str) -> bool:
    """Есть ли уже предложение того же действия для этого объявления, ждущее решения."""
    return conn.execute(
        "SELECT 1 FROM changes WHERE unit_id = ? AND action = ? AND status = 'proposed'", (unit_id, action)
    ).fetchone() is not None


def _lead_cost_text(spend: int, leads: int) -> str:
    if not leads:
        return "обращений нет"
    return f"{plural(leads, 'обращение', 'обращения', 'обращений')}, одно обходится примерно в {rub(spend / leads)}"


_PROPOSAL_WORDS = [
    ("Добавили второй вариант", "Добавим второй вариант"),
    ("Второе объявление остановили.", "Второе объявление остановим."),
    ("Остановили ", "Предлагаем остановить "),
    ("Добавили ", "Предлагаем добавить "),
    ("Запустили ", "Предлагаем запустить "),
    ("Оставили ", "Предлагаем оставить "),
]


def proposal_text(text: str) -> str:
    """Тот же текст, но в форме предложения: «Остановили…» → «Предлагаем остановить…»."""
    for old, new in _PROPOSAL_WORDS:
        text = text.replace(old, new)
    return text


def _propose(conn, biz, unit_id, today, action, code, text, before, after, evidence=None) -> int:
    """Записывает предложение: в кабинете ничего не меняется, пока владелец не подтвердит."""
    evidence = {**(evidence or {}), "done_text": text}
    return log_change(conn, biz["id"], unit_id, today, "auto", action, code, proposal_text(text),
                      before, after, evidence, status="proposed")


def _pause(conn, platform, biz, unit, today, code, text, evidence=None, propose=False, actor="auto") -> int | None:
    """Останавливает объявление. В режиме «спрашивать» только записывает предложение (None, если оно уже есть)."""
    if propose:
        if has_open_proposal(conn, unit["id"], "pause"):
            return None
        return _propose(conn, biz, unit["id"], today, "pause", code, text, {"state": "active"}, {"state": "paused"}, evidence)
    platform.set_state(unit["id"], "paused", today)
    return log_change(
        conn, biz["id"], unit["id"], today, actor, "pause", code, text,
        {"state": "active"}, {"state": "paused"}, evidence,
    )


def _finish_experiments(conn, platform, biz, today, as_of, cfg, propose=False) -> list[int]:
    made = []
    for ex in conn.execute("SELECT * FROM experiments WHERE business_id = ? AND status = 'running'", (biz["id"],)).fetchall():
        a = conn.execute("SELECT * FROM ad_units WHERE id = ?", (ex["unit_a"],)).fetchone()
        b = conn.execute("SELECT * FROM ad_units WHERE id = ?", (ex["unit_b"],)).fetchone()
        waiting = {"draft", "moderation", "unpaid"}
        if (a["state"] in waiting or b["state"] in waiting) and days_between(ex["started_day"], today) < 10:
            continue  # второй вариант ещё проверяет Яндекс или ждёт оплаты: тест не начался
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
        change_id = _pause(
            conn, platform, biz, loser, today, "ab_loser", text,
            {"verdict": verdict, "p_b_better": round(p, 3), "a": sa, "b": sb, "experiment_id": ex["id"]},
            propose=propose,
        )
        if change_id is not None:
            made.append(change_id)
        if not propose:  # в режиме «спрашивать» тест закрывается, когда владелец подтвердит вывод
            conn.execute(
                "UPDATE experiments SET status = 'done', verdict = ?, p_better = ?, ended_day = ? WHERE id = ?",
                (verdict, p, today, ex["id"]),
            )
    return made


def run_cycle(
    conn: sqlite3.Connection, biz: sqlite3.Row, platform: Platform, today: str,
    cfg: EngineConfig = EngineConfig(), propose: bool | None = None, decide: bool = True,
) -> list[int]:
    """Выполняет один цикл и возвращает номера созданных записей истории.

    propose=True (режим «сначала спрашивать»): изменения только предлагаются и ждут подтверждения.
    Защита бюджета от режима не зависит и действует всегда."""
    if propose is None:
        propose = "mode" in biz.keys() and biz["mode"] == "ask"
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

    if not decide:
        # Нет данных об обращениях (Метрика не подключена): судить о рекламе нечем, работает только защита бюджета.
        conn.commit()
        return made

    # 2. Завершённые A/B-тесты.
    made += _finish_experiments(conn, platform, biz, today, as_of, cfg, propose=propose)
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
            change_id = _pause(
                conn, platform, biz, u, today, "bad_unit",
                f"Остановили «{u['title']}»: потратили {rub(s['spend'])}, {_lead_cost_text(s['spend'], s['leads'])}. "
                f"Вы готовы платить за обращение не больше {rub(lead_target)}.",
                evidence(e), propose=propose,
            )
            if change_id is not None:
                made.append(change_id)
            if not propose:
                free += u["budget_micro"]  # бюджет освободится, только когда остановка состоится

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
            if propose and has_open_proposal(conn, u["id"], "set_budget"):
                continue
            free -= new - u["budget_micro"]
            s = e["stats"]
            text = (
                f"Добавили {round(cfg.raise_step * 100)}% бюджета для «{u['title']}»: {_lead_cost_text(s['spend'], s['leads'])}, "
                f"это дешевле вашей цели ({rub(lead_target)}). Бюджет на неделю: {rub(u['budget_micro'])} → {rub(new)}."
            )
            before, after = {"budget_micro": u["budget_micro"]}, {"budget_micro": new}
            if propose:
                made.append(_propose(conn, biz, u["id"], today, "set_budget", "good_unit", text, before, after, evidence(e)))
            else:
                platform.set_budget(u["id"], new, today)
                made.append(log_change(conn, biz["id"], u["id"], today, "auto", "set_budget", "good_unit", text, before, after, evidence(e)))

        # 6. Пограничным даём шанс: сравниваем с новым вариантом.
        for e in evals:
            u = e["unit"]
            if e["cls"] != "borderline" or e["prev"] != "borderline" or cooling(u):
                continue
            if u["id"] in in_test or u["id"] in tested_before or u["variant_of"] is not None:
                continue
            if u["budget_micro"] > free:
                continue
            if propose and has_open_proposal(conn, u["id"], "start_test"):
                continue
            s = e["stats"]
            if propose:
                free -= u["budget_micro"]
                made.append(_propose(
                    conn, biz, u["id"], today, "start_test", "borderline_unit",
                    f"Запустили сравнение для «{u['title']}»: пока не ясно, окупается ли оно ({_lead_cost_text(s['spend'], s['leads'])}, "
                    f"цель {rub(lead_target)}). Добавили второй вариант с таким же бюджетом.",
                    {}, {}, {**evidence(e), "free_text": (
                        f"пока не ясно, окупается ли оно ({_lead_cost_text(s['spend'], s['leads'])}, цель {rub(lead_target)})"
                    )},
                ))
                continue
            made.append(_start_test(conn, platform, biz, u, today, free_text=(
                f"пока не ясно, окупается ли оно ({_lead_cost_text(s['spend'], s['leads'])}, цель {rub(lead_target)})"
            ), evidence=evidence(e)))
            free -= u["budget_micro"]

    conn.commit()
    return made


def _start_test(conn, platform, biz, unit, today, free_text: str, evidence=None, change_id: int | None = None) -> int:
    """Создаёт второй вариант объявления и запускает сравнение. Если change_id задан, дополняет им уже записанное предложение."""
    variant_id = platform.create_variant(unit["id"], today)
    variant = conn.execute("SELECT title FROM ad_units WHERE id = ?", (variant_id,)).fetchone()
    ex = conn.execute(
        "INSERT INTO experiments (business_id, unit_a, unit_b, started_day) VALUES (?,?,?,?)",
        (biz["id"], unit["id"], variant_id, today),
    )
    platform.touch(unit["id"], today)  # объявление не менялось, но тест начался: даём ему «остыть»
    after = {"variant_unit_id": variant_id, "experiment_id": ex.lastrowid}
    text = (
        f"Запустили сравнение для «{unit['title']}»: {free_text}. "
        f"Добавили второй вариант «{variant['title']}» с таким же бюджетом."
    )
    if change_id is not None:
        conn.execute(
            "UPDATE changes SET after_json = ?, reason_text = ?, status = 'applied' WHERE id = ?",
            (json.dumps(after, ensure_ascii=False), text, change_id),
        )
        return change_id
    return log_change(conn, biz["id"], unit["id"], today, "auto", "start_test", "borderline_unit", text, {}, after, evidence)


def rollback(conn: sqlite3.Connection, platform: Platform, biz: sqlite3.Row, change_id: int, today: str, force: bool = False) -> int:
    """Отменяет изменение. Возвращает номер новой записи в истории."""
    ch = conn.execute("SELECT * FROM changes WHERE id = ? AND business_id = ?", (change_id, biz["id"])).fetchone()
    if ch is None:
        raise EngineError("Такое изменение не найдено.", "not_found")
    if ch["reverted_by"]:
        raise EngineError("Это изменение уже отменено.", "already")
    if ch["status"] != "applied":
        raise EngineError("Это предложение ещё не выполнено, отменять нечего. Его можно подтвердить или отклонить.", "not_allowed")
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


# ---------- решения владельца: подтвердить, отклонить, остановить всё ----------

def _open_proposal(conn: sqlite3.Connection, biz: sqlite3.Row, change_id: int) -> sqlite3.Row:
    ch = conn.execute("SELECT * FROM changes WHERE id = ? AND business_id = ?", (change_id, biz["id"])).fetchone()
    if ch is None:
        raise EngineError("Такое предложение не найдено.", "not_found")
    if ch["status"] != "proposed":
        raise EngineError("Это предложение уже не ждёт решения.", "already")
    return ch


def approve(conn: sqlite3.Connection, platform: Platform, biz: sqlite3.Row, change_id: int, today: str,
            cfg: EngineConfig = EngineConfig()) -> int:
    """Выполняет предложение. Если с тех пор всё изменилось, предложение закрывается как устаревшее."""
    ch = _open_proposal(conn, biz, change_id)
    before, after = json.loads(ch["before_json"]), json.loads(ch["after_json"])
    evidence = json.loads(ch["evidence_json"])
    unit = conn.execute("SELECT * FROM ad_units WHERE id = ?", (ch["unit_id"],)).fetchone()

    def stale(message: str):
        conn.execute("UPDATE changes SET status = 'rejected' WHERE id = ?", (change_id,))
        conn.commit()
        raise EngineError(message + " Предложение закрыто, сервис предложит новое, если нужно.", "stale")

    if unit is None or unit["state"] != "active":
        stale("Объявление уже остановлено или удалено.")
    cap = int((1 - cfg.reserve) * biz["weekly_budget_micro"])
    active_total = conn.execute(
        "SELECT COALESCE(SUM(budget_micro),0) FROM ad_units WHERE business_id = ? AND state = 'active'", (biz["id"],)
    ).fetchone()[0]

    if ch["action"] == "pause":
        platform.set_state(unit["id"], "paused", today)
        if evidence.get("experiment_id"):
            conn.execute(
                "UPDATE experiments SET status = 'done', verdict = ?, p_better = ?, ended_day = ? WHERE id = ?",
                (evidence.get("verdict"), evidence.get("p_b_better"), today, evidence["experiment_id"]),
            )
    elif ch["action"] == "set_budget":
        if unit["budget_micro"] != before["budget_micro"]:
            stale("Бюджет этого объявления уже менялся.")
        if cap > 0 and active_total + after["budget_micro"] - before["budget_micro"] > cap:
            stale("Недельный бюджет уже распределён, добавить больше нельзя.")
        platform.set_budget(unit["id"], after["budget_micro"], today)
    elif ch["action"] == "start_test":
        if conn.execute("SELECT 1 FROM experiments WHERE unit_a = ? OR unit_b = ?", (unit["id"], unit["id"])).fetchone():
            stale("Для этого объявления сравнение уже было.")
        if cap > 0 and active_total + unit["budget_micro"] > cap:
            stale("Недельный бюджет уже распределён, второй вариант не поместится.")
        return _start_test(conn, platform, biz, unit, today, evidence.get("free_text", "пока не ясно, окупается ли оно"), change_id=change_id)
    else:
        raise EngineError("Это действие нельзя подтвердить.", "not_allowed")

    conn.execute(
        "UPDATE changes SET status = 'applied', reason_text = ? WHERE id = ?",
        (evidence.get("done_text") or ch["reason_text"], change_id),
    )
    conn.commit()
    return change_id


def reject(conn: sqlite3.Connection, biz: sqlite3.Row, change_id: int, today: str) -> None:
    """Отклоняет предложение. Ничего в рекламе не меняется, объявление не трогаем ещё несколько дней."""
    ch = _open_proposal(conn, biz, change_id)
    evidence = json.loads(ch["evidence_json"])
    conn.execute("UPDATE changes SET status = 'rejected' WHERE id = ?", (change_id,))
    if evidence.get("experiment_id"):  # владелец оставляет оба варианта
        conn.execute("UPDATE experiments SET status = 'cancelled', ended_day = ? WHERE id = ?", (today, evidence["experiment_id"]))
    if ch["unit_id"]:
        conn.execute("UPDATE ad_units SET last_changed_day = ? WHERE id = ?", (today, ch["unit_id"]))
    conn.commit()


def stop_all(conn: sqlite3.Connection, platform: Platform, biz: sqlite3.Row, today: str) -> int:
    """Стоп-кран: останавливает всю рекламу бизнеса. Открытые предложения закрываются."""
    conn.execute("UPDATE changes SET status = 'rejected' WHERE business_id = ? AND status = 'proposed'", (biz["id"],))
    units = conn.execute("SELECT * FROM ad_units WHERE business_id = ? AND state = 'active'", (biz["id"],)).fetchall()
    for u in units:
        _pause(conn, platform, biz, u, today, "user_stop_all",
               f"Вы остановили всю рекламу: «{u['title']}» больше не показывается.", actor="user")
    # Пока стоп-кран нажат, объявления, которые позже одобрит Яндекс или оплатит владелец, тоже не запускаются.
    conn.execute("UPDATE businesses SET status = 'stopped' WHERE id = ?", (biz["id"],))
    conn.commit()
    return len(units)


def resume_all(conn: sqlite3.Connection, platform: Platform, biz: sqlite3.Row, today: str) -> int:
    """Возвращает то, что остановил стоп-кран (откатывает эти остановки)."""
    rows = conn.execute(
        "SELECT id FROM changes WHERE business_id = ? AND reason_code = 'user_stop_all' AND status = 'applied' "
        "AND reverted_by IS NULL ORDER BY id", (biz["id"],),
    ).fetchall()
    for row in rows:
        rollback(conn, platform, biz, row["id"], today, force=True)
    conn.execute("UPDATE businesses SET status = 'running' WHERE id = ?", (biz["id"],))
    conn.commit()
    return len(rows)
