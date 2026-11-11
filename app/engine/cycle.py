"""Один цикл бидера: оценка объявлений, решения, применение и запись в историю.

Правила описаны в docs/algorithms.md. Движок работает с любой площадкой через
интерфейс Platform и не знает, настоящие данные или пример.
"""
import json
import math
import random
import sqlite3

from app.engine.config import EngineConfig
from app.engine.stats import classify, enough_data, prob_b_better, prob_ok
from app.engine.util import MICRO, EngineError, day_add, days_between, plural, rub
from app.platforms.base import Platform

CLASS_LABELS = {
    "good": "Окупается",
    "borderline": "Пока неясно",
    "bad": "Не окупается",
    "waiting": "Собираем данные",
}


def total_budget(conn: sqlite3.Connection, biz: sqlite3.Row) -> int:
    """Недельный бюджет всей рекламы бизнеса. В Директе бюджет задан на каждую группу объявлений (у каждой своя кампания),
    поэтому всего это бюджет группы, умноженный на число кампаний. Для примера и других площадок кампания одна."""
    campaigns = conn.execute("SELECT COUNT(*) FROM campaigns WHERE business_id = ? AND external_id IS NOT NULL", (biz["id"],)).fetchone()[0]
    return biz["weekly_budget_micro"] * max(1, campaigns)


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


def log_change(conn, business_id, unit_id, day, actor, action, code, text, before, after, evidence=None, status="applied",
               campaign_id=None) -> int:
    """campaign_id: изменение касается всей кампании (бюджет, стратегия), а не одного объявления (тогда unit_id пустой)."""
    cur = conn.execute(
        "INSERT INTO changes (business_id, unit_id, campaign_id, day, actor, action, reason_code, reason_text, "
        "before_json, after_json, evidence_json, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            business_id, unit_id, campaign_id, day, actor, action, code, text,
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


def has_open_campaign_proposal(conn, campaign_id: int) -> bool:
    """Есть ли уже предложение изменить эту кампанию (бюджет или стратегию), ждущее решения."""
    return conn.execute("SELECT 1 FROM changes WHERE campaign_id = ? AND status = 'proposed'", (campaign_id,)).fetchone() is not None


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
    ("Перевели ", "Предлагаем перевести "),
]


def proposal_text(text: str) -> str:
    """Тот же текст, но в форме предложения: «Остановили…» → «Предлагаем остановить…»."""
    for old, new in _PROPOSAL_WORDS:
        text = text.replace(old, new)
    return text


def _propose(conn, biz, unit_id, today, action, code, text, before, after, evidence=None, campaign_id=None) -> int:
    """Записывает предложение: в кабинете ничего не меняется, пока владелец не подтвердит."""
    evidence = {**(evidence or {}), "done_text": text}
    return log_change(conn, biz["id"], unit_id, today, "auto", action, code, proposal_text(text),
                      before, after, evidence, status="proposed", campaign_id=campaign_id)


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


def _cohort_step(conn, platform, biz, today, as_of, cfg, propose=False) -> list[int]:
    """Объявления, запущенные в один день каждое в своей кампании, сравниваем между собой: через cohort_days дней слабое останавливаем,
    а его недельный лимит переходит к лучшему. Судим по обращениям на клик, а когда обращений в группе меньше двух (цель не измеряется) по доле кликов
    от показов: это единственное, что можно сравнить. Не хватило уверенности: ждём до cohort_max_days и тогда оставляем лучшее по цифрам."""
    rows = conn.execute(
        "SELECT u.*, c.id AS camp_id, c.weekly_micro AS camp_weekly, c.created_day AS camp_day, "
        "(SELECT COUNT(*) FROM ad_units x WHERE x.campaign_id = c.id) AS in_campaign FROM ad_units u JOIN campaigns c ON c.id = u.campaign_id "
        "WHERE u.business_id = ? AND c.source = 'direct' AND c.external_id IS NOT NULL AND c.state != 'paused' AND u.state = 'active' AND u.ad_kind != 'responsive' "
        "ORDER BY c.created_day, u.id", (biz["id"],)).fetchall()
    in_test = {uid for r in conn.execute("SELECT unit_a, unit_b FROM experiments WHERE business_id = ? AND status = 'running'", (biz["id"],)) for uid in (r["unit_a"], r["unit_b"])}
    groups: dict[str, list] = {}
    for r in rows:
        if r["in_campaign"] == 1 and r["id"] not in in_test and r["camp_day"]:   # объявление одно на кампанию: именно такие запускаются «одновременно, но порознь»
            groups.setdefault(r["camp_day"], []).append(r)
    made: list[int] = []
    cap = int((1 - cfg.reserve) * biz["weekly_budget_micro"])
    for start, members in groups.items():
        age = days_between(start, today)
        if len(members) < 2 or age < cfg.cohort_days:
            continue
        stats = {m["id"]: unit_stats(conn, m["id"], start, as_of) for m in members}
        measured = sum(s["leads"] for s in stats.values()) >= 2
        trials = (lambda s: s["clicks"]) if measured else (lambda s: s["impressions"])
        wins = (lambda s: s["leads"]) if measured else (lambda s: s["clicks"])
        if any(trials(s) == 0 or s["clicks"] < cfg.cohort_min_clicks for s in stats.values()) and age < cfg.cohort_max_days:
            continue
        best = max(members, key=lambda m, stats=stats, wins=wins, trials=trials: (wins(stats[m["id"]]) + 1) / (trials(stats[m["id"]]) + 2))
        sb = stats[best["id"]]
        best_weekly = best["camp_weekly"]
        for m in members:
            if m["id"] == best["id"] or has_open_proposal(conn, m["id"], "pause"):
                continue
            sm = stats[m["id"]]
            p = prob_b_better(trials(sm), wins(sm), trials(sb), wins(sb), random.Random(f"cohort:{m['id']}:{today}"), cfg.samples)
            if p < cfg.cohort_confidence and age < cfg.cohort_max_days:
                continue
            what = "обращений на клик" if measured else "кликов на показ"
            fmt = (lambda s: f"{s['leads'] / s['clicks'] * 100:.1f} обращений на 100 кликов" if s["clicks"] else "кликов нет") if measured else \
                  (lambda s: f"CTR {s['clicks'] / s['impressions'] * 100:.2f}%" if s["impressions"] else "показов нет")
            text = (f"Остановили «{m['title']}»: из объявлений, запущенных одновременно, оно сработало слабее ({what}) за {plural(age, 'день', 'дня', 'дней')}: "
                    f"{fmt(sm)} против {fmt(sb)} у «{best['title']}». "
                    + ("" if measured else "Обращения пока не измеряются, поэтому сравнили по кликам: подключите цель Метрики, и сравнение пойдёт по обращениям. ")
                    + f"Лимит остановленной кампании ({rub(m['camp_weekly'])} в неделю) уходит лучшему.")
            evidence = {"cohort": True, "metric": "leads" if measured else "ctr", "p_better": round(p, 3), "winner_unit": best["id"], "loser": sm, "winner": sb}
            change_id = _pause(conn, platform, biz, m, today, "cohort_loser", text, evidence, propose=propose)
            if change_id is None:
                continue
            made.append(change_id)
            new = min(best_weekly + m["camp_weekly"], cap)
            if new > best_weekly and not has_open_campaign_proposal(conn, best["camp_id"]):
                move = (f"Добавили бюджет кампании «{best['title']}»: к ней перешёл лимит остановленного объявления. "
                        f"Бюджет на неделю: {rub(best_weekly)} → {rub(new)}.")
                before, after = {"campaign_budget_micro": best_weekly}, {"campaign_budget_micro": new}
                if propose:
                    made.append(_propose(conn, biz, None, today, "set_budget", "cohort_winner", move, before, after, {"cohort": True}, campaign_id=best["camp_id"]))
                else:
                    platform.set_campaign_budget(best["camp_id"], new, today)
                    made.append(log_change(conn, biz["id"], None, today, "auto", "set_budget", "cohort_winner", move, before, after, {"cohort": True}, campaign_id=best["camp_id"]))
                best_weekly = new
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
    budget_total = total_budget(conn, biz)
    made: list[int] = []

    def active_units():
        return conn.execute("SELECT * FROM ad_units WHERE business_id = ? AND state = 'active' ORDER BY id", (biz["id"],)).fetchall()

    shared = bool(getattr(platform, "shared_budget", False))   # объявления делят бюджет одной кампании (Директ)
    units = active_units()
    spent_by = conn.execute("SELECT * FROM ad_units WHERE business_id = ?", (biz["id"],)).fetchall() if shared else units   # остановленное тоже тратило
    week_spend = sum(unit_stats(conn, u["id"], day_add(as_of, -6), as_of)["spend"] for u in spent_by)

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

    if shared:   # объявления в отдельных кампаниях, запущенные вместе: слабое уходит через несколько дней (оценке обращений тут нужны не всегда, поэтому до проверки decide)
        made += _cohort_step(conn, platform, biz, today, as_of, cfg, propose)

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
            early = cfg.early_days > 0 and days_between(u["created_day"], today) < cfg.early_days   # первые дни: кликов мало, судим строже
            if s["leads"] == 0 and s["spend"] >= (cfg.early_zero_factor if early else 2) * lead_target:
                cls = "bad"  # потратили в несколько раз больше цены обращения, а обращений нет
            elif early and cls == "bad" and p > cfg.early_p_bad:
                cls = "borderline"  # по обычному правилу слабое, но данных ещё мало для такого вывода
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
    working: dict = {}   # сколько объявлений ещё работает в каждой кампании (в общей кампании последнее не останавливаем никогда)
    for u in units:
        working[u["campaign_id"]] = working.get(u["campaign_id"], 0) + 1
    for e in evals:
        u = e["unit"]
        if e["cls"] == "bad" and e["prev"] == "bad" and not cooling(u) and u["id"] not in in_test:
            if shared and working[u["campaign_id"]] <= 1:
                continue
            s = e["stats"]
            change_id = _pause(
                conn, platform, biz, u, today, "bad_unit",
                f"Остановили «{u['title']}»: потратили {rub(s['spend'])}, {_lead_cost_text(s['spend'], s['leads'])}. "
                f"Вы готовы платить за обращение не больше {rub(lead_target)}.",
                evidence(e), propose=propose,
            )
            if change_id is not None:
                made.append(change_id)
                working[u["campaign_id"]] -= 1   # и предложенную остановку считаем: подтвердят все, последнее объявление кампании останется
            if not propose:
                free += u["budget_micro"]  # бюджет освободится, только когда остановка состоится

    if shared:
        # 5-6. В общей кампании бюджет и стратегия общие: решаем про кампанию целиком. Отдельных сравнений не запускаем:
        # варианты и так показываются в одних аукционах, и слабый уходит по правилу выше.
        for camp in conn.execute("SELECT * FROM campaigns WHERE business_id = ? AND external_id IS NOT NULL ORDER BY id", (biz["id"],)).fetchall():
            made += _campaign_step(conn, platform, biz, today, as_of, cfg, lead_target, cpa, propose, block_growth, camp)
    # 5. Хорошим добавляем бюджет, начиная с самых надёжных.
    elif not block_growth:
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


def _window(conn, unit_ids: list[int], start: str, end: str) -> dict:
    """Суммарная статистика объявлений за период."""
    total = {"impressions": 0, "clicks": 0, "spend": 0, "leads": 0, "clients": 0}
    for uid in unit_ids:
        s = unit_stats(conn, uid, start, end)
        for k in total:
            total[k] += s[k]
    return total


def _campaign_step(conn, platform, biz, today, as_of, cfg, lead_target, cpa, propose, block_growth, camp) -> list[int]:
    """Решения про одну кампанию (группу объявлений): перевести на «Максимум конверсий», когда обращений достаточно, и осторожно добавить бюджет.
    Потолок бюджета у каждой кампании свой: бюджет группы за вычетом запаса.

    Яндекс учится на данных кампании и сбивается от частых правок, поэтому после запуска её неделю не трогаем
    (warmup_days), а дальше правим не чаще раза в неделю (cooldown_days) и не больше чем на raise_step."""
    if camp["state"] != "active":
        return []
    if camp["created_day"] and days_between(camp["created_day"], today) < cfg.warmup_days:
        return []
    if camp["last_changed_day"] and days_between(camp["last_changed_day"], today) < cfg.cooldown_days:
        return []
    if has_open_campaign_proposal(conn, camp["id"]):
        return []
    unit_ids = [r["id"] for r in conn.execute("SELECT id FROM ad_units WHERE campaign_id = ?", (camp["id"],))]
    weekly = camp["weekly_micro"]

    def decide(action, code, text, before, after, evidence, apply) -> list[int]:
        if propose:
            return [_propose(conn, biz, None, today, action, code, text, before, after, evidence, campaign_id=camp["id"])]
        apply()
        return [log_change(conn, biz["id"], None, today, "auto", action, code, text, before, after, evidence, campaign_id=camp["id"])]

    # 1. Хватает обращений: Яндекс подбирает ставки по обращениям, а не по кликам.
    week = _window(conn, unit_ids, day_add(as_of, -6), as_of)
    if camp["strategy"] == "clicks" and biz["metrika_goal_id"] and week["leads"] >= cfg.switch_leads:
        text = (f"Перевели кампанию на «Максимум конверсий»: за неделю набралось {plural(week['leads'], 'обращение', 'обращения', 'обращений')}, "
                "Яндексу хватает данных, чтобы подбирать ставки под обращения, а не под клики. "
                f"Недельный бюджет прежний: {rub(weekly)}.")
        return decide("set_strategy", "enough_leads", text, {"strategy": "clicks"}, {"strategy": "conversions"},
                      {"leads_week": week["leads"], "clicks_week": week["clicks"], "spend_week": week["spend"]},
                      lambda: platform.set_strategy(camp["id"], "conversions", today))

    # 2. Кампания окупается две оценки подряд: добавляем немного бюджета.
    if block_growth:
        return []
    cap = int((1 - cfg.reserve) * biz["weekly_budget_micro"])
    new = min(int(weekly * (1 + cfg.raise_step)), cap)
    if new <= weekly:
        return []

    def judge(end: str):
        w = _window(conn, unit_ids, day_add(end, -(cfg.window_days - 1)), end)
        if not enough_data(w["clicks"], w["spend"], cpa, cfg):
            return "waiting", 0.0, w
        p = prob_ok(w["clicks"], w["leads"], w["spend"], lead_target, random.Random(f"camp:{camp['id']}:{end}"), cfg.samples)
        return classify(p, cfg), p, w

    now_cls, p, w = judge(as_of)
    if now_cls != "good" or judge(day_add(as_of, -1))[0] != "good":
        return []
    pct = round((new / weekly - 1) * 100)
    text = (f"Добавили {pct}% бюджета кампании: {_lead_cost_text(w['spend'], w['leads'])}, это дешевле вашей цели ({rub(lead_target)}). "
            f"Бюджет на неделю: {rub(weekly)} → {rub(new)}.")
    return decide("set_budget", "good_campaign", text, {"campaign_budget_micro": weekly}, {"campaign_budget_micro": new},
                  {"clicks": w["clicks"], "leads": w["leads"], "spend": w["spend"], "p_ok": round(p, 3), "target_lead": lead_target},
                  lambda: platform.set_campaign_budget(camp["id"], new, today))


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
        conn.commit()
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
    elif ch["action"] == "start_ab":
        platform.rewrite_ad(ch["unit_id"], before["title"], before["text"], before.get("angle", ""), today)
        conn.execute("UPDATE experiments SET status = 'cancelled', ended_day = ? WHERE id = ?", (today, after["experiment_id"]))
        conn.execute("UPDATE ad_units SET variant_of = NULL WHERE id = ?", (ch["unit_id"],))
        text = "Вы отменили сравнение: вернули прежний текст объявления."
        new_before, new_after = after, before
    elif ch["campaign_id"] is not None:
        camp = conn.execute("SELECT * FROM campaigns WHERE id = ?", (ch["campaign_id"],)).fetchone()
        if camp is None:
            raise EngineError("Кампании уже нет, отменять нечего.", "not_found")
        current = {"strategy": camp["strategy"], "campaign_budget_micro": camp["weekly_micro"]}
        if any(current[k] != v for k, v in after.items()) and not force:
            raise EngineError(
                "С тех пор эту кампанию меняли ещё раз. Если отменить сейчас, вернётся состояние до этого изменения. Подтвердите отмену.",
                "conflict",
            )
        if "campaign_budget_micro" in before:
            platform.set_campaign_budget(camp["id"], before["campaign_budget_micro"], today)
        if "strategy" in before:
            platform.set_strategy(camp["id"], before["strategy"], today)
        text = "Вы отменили изменение кампании: вернули как было."
        new_before, new_after = {k: current[k] for k in before}, before
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
                        {"original_change_id": change_id}, campaign_id=ch["campaign_id"])
    conn.execute("UPDATE changes SET reverted_by = ? WHERE id = ?", (new_id, change_id))
    conn.commit()
    return new_id


# ---------- решения владельца: подтвердить, отклонить, остановить всё ----------

def set_budget_by_owner(conn: sqlite3.Connection, platform: Platform, biz: sqlite3.Row, campaign_id: int, weekly_micro: int, today: str,
                        minimum: int, cfg: EngineConfig = EngineConfig()) -> int | None:
    """Владелец сам меняет недельный бюджет кампании. Записывается в историю (можно отменить). Возвращает номер записи
    или None, если бюджет тот же. Если новый бюджет не помещается в потолок недели (за вычетом запаса), общий бюджет бизнеса
    поднимается следом: иначе потолок не даст сервису ничего менять в этой кампании и счётчик «потрачено из» показывал бы больше 100%."""
    camp = conn.execute("SELECT * FROM campaigns WHERE id = ? AND business_id = ?", (campaign_id, biz["id"])).fetchone()
    if camp is None or camp["external_id"] is None:
        raise EngineError("Такой кампании в Директе нет.", "not_found")
    old = camp["weekly_micro"]
    if weekly_micro == old:
        return None
    if weekly_micro < minimum:
        raise EngineError(f"Меньше {rub(minimum)} в неделю Яндекс на кампанию не принимает.", "budget_too_small")
    platform.set_campaign_budget(campaign_id, weekly_micro, today)
    if weekly_micro > int((1 - cfg.reserve) * biz["weekly_budget_micro"]):
        needed = math.ceil(weekly_micro / (1 - cfg.reserve) / MICRO) * MICRO
        conn.execute("UPDATE businesses SET weekly_budget_micro = ? WHERE id = ?", (needed, biz["id"]))
    text = f"Вы изменили недельный бюджет кампании: {rub(old)} → {rub(weekly_micro)}."
    change_id = log_change(conn, biz["id"], None, today, "user", "set_budget", "user_budget", text,
                           {"campaign_budget_micro": old}, {"campaign_budget_micro": weekly_micro}, campaign_id=campaign_id)
    conn.commit()
    return change_id


def set_unit_paused_by_owner(conn: sqlite3.Connection, platform: Platform, biz: sqlite3.Row, unit_id: int, pause: bool, today: str) -> int:
    """Владелец сам ставит объявление на паузу или включает обратно. Записывается в историю (можно отменить). Возвращает номер записи."""
    unit = conn.execute("SELECT * FROM ad_units WHERE id = ? AND business_id = ?", (unit_id, biz["id"])).fetchone()
    if unit is None:
        raise EngineError("Такого объявления нет.", "not_found")
    if pause:
        if unit["state"] == "paused":
            raise EngineError("Объявление уже на паузе.", "already")
        if unit["state"] in ("draft", "rejected"):
            raise EngineError("Это объявление и так не показывается: оно не прошло проверку или ещё не отправлено.", "not_allowed")
    else:
        if unit["state"] != "paused":
            raise EngineError("Объявление не на паузе.", "already")
        if biz["status"] == "stopped":
            raise EngineError("Сейчас нажат стоп-кран. Сначала верните рекламу кнопкой «Включить обратно», тогда объявления можно включать по одному.", "not_allowed")
    platform.set_state(unit_id, "paused" if pause else "active", today)
    text = f"Вы поставили на паузу «{unit['title']}»: оно больше не показывается." if pause else f"Вы снова включили «{unit['title']}»."
    before, after = {"state": unit["state"]}, {"state": "paused" if pause else "active"}
    change_id = log_change(conn, biz["id"], unit_id, today, "user", "pause" if pause else "resume", "user_pause" if pause else "user_resume", text, before, after)
    conn.commit()
    return change_id


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

    def stale(message: str):
        conn.execute("UPDATE changes SET status = 'rejected' WHERE id = ?", (change_id,))
        conn.commit()
        raise EngineError(message + " Предложение закрыто, сервис предложит новое, если нужно.", "stale")

    cap = int((1 - cfg.reserve) * biz["weekly_budget_micro"])
    if ch["campaign_id"] is not None:   # бюджет или стратегия всей кампании
        camp = conn.execute("SELECT * FROM campaigns WHERE id = ?", (ch["campaign_id"],)).fetchone()
        if camp is None or camp["state"] != "active":
            stale("Кампания сейчас не работает.")
        if ch["action"] == "set_strategy":
            if camp["strategy"] != before["strategy"]:
                stale("Стратегия кампании уже менялась.")
            platform.set_strategy(camp["id"], after["strategy"], today)
        elif ch["action"] == "set_budget":
            if camp["weekly_micro"] != before["campaign_budget_micro"]:
                stale("Бюджет кампании уже менялся.")
            if cap > 0 and after["campaign_budget_micro"] > cap:
                stale("Недельный бюджет уже распределён, добавить больше нельзя.")
            platform.set_campaign_budget(camp["id"], after["campaign_budget_micro"], today)
        else:
            raise EngineError("Это действие нельзя подтвердить.", "not_allowed")
        conn.execute("UPDATE changes SET status = 'applied', reason_text = ? WHERE id = ?", (evidence.get("done_text") or ch["reason_text"], change_id))
        conn.commit()
        return change_id

    cap = int((1 - cfg.reserve) * total_budget(conn, biz))   # у объявления потолок общий на всю рекламу, у кампании выше был свой
    unit = conn.execute("SELECT * FROM ad_units WHERE id = ?", (ch["unit_id"],)).fetchone()
    if unit is None or unit["state"] != "active":
        stale("Объявление уже остановлено или удалено.")
    if ch["action"] == "pause" and not evidence.get("cohort") and getattr(platform, "shared_budget", False) and conn.execute(
            "SELECT COUNT(*) FROM ad_units WHERE campaign_id = ? AND state = 'active'", (unit["campaign_id"],)).fetchone()[0] <= 1:
        stale("Это последнее работающее объявление кампании, его остановить нельзя.")
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
    elif ch["action"] == "start_ab":
        best = conn.execute("SELECT * FROM ad_units WHERE id = ?", (evidence.get("best_unit_id"),)).fetchone()
        if best is None or best["state"] != "active":
            stale("Лучшее объявление группы уже остановлено.")
        if conn.execute("SELECT 1 FROM experiments WHERE business_id = ? AND status = 'running'", (biz["id"],)).fetchone():
            stale("Другое сравнение уже идёт.")
        v = evidence["variant"]
        platform.rewrite_ad(unit["id"], v["title"], v["text"], v.get("angle", ""), today)
        ex = conn.execute("INSERT INTO experiments (business_id, unit_a, unit_b, started_day) VALUES (?,?,?,?)", (biz["id"], best["id"], unit["id"], today))
        conn.execute("UPDATE ad_units SET variant_of = ? WHERE id = ?", (best["id"], unit["id"]))
        conn.execute("UPDATE changes SET after_json = ? WHERE id = ?",
                     (json.dumps({"experiment_id": ex.lastrowid, "title": v["title"], "text": v["text"]}, ensure_ascii=False), change_id))
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
    if ch["campaign_id"] is not None:   # кампанию не трогаем ещё неделю, предложение не возвращается на следующий день
        conn.execute("UPDATE campaigns SET last_changed_day = ? WHERE id = ?", (today, ch["campaign_id"]))
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
