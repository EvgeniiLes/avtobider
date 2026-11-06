"""Тексты как у маркетолога: короткий текст для баннера (hook), набор кандидатов и отбор судьёй, данные рекламы в промпте."""
import asyncio
import json
from datetime import date

import httpx

from app.creative import texts_ai
from app.creative.strategist import plan_briefs
from app.db import connect, init_db
from app.direct.textctx import texts_context
from app.direct.texts import check_hook
from app.engine.util import MICRO, day_add

BIZ = {"name": "Лотос", "kind": "Маникюрный салон", "city": "Москва", "description": "Маникюр и педикюр в центре города. Работаем ежедневно.", "tone": "",
       "address": "", "districts": "", "nationwide": 0, "allow_promo": 0, "maps_url": "", "visual_hint": "", "audience": "", "id": 1}

CANDIDATES = [
    {"title": "Маникюр в центре Москвы", "text": "Запишитесь онлайн на удобное время", "hook": "Маникюр в центре", "angle": "booking", "why": "запрос «маникюр центр»"},
    {"title": "Педикюр в центре города", "text": "Работаем ежедневно, приходите без очереди", "hook": "Педикюр ежедневно", "angle": "comfort", "why": "удобство"},
    {"title": "Маникюр и педикюр рядом", "text": "Салон в центре, запись в пару кликов", "hook": "Салон в центре!", "angle": "booking", "why": "расположение"},
    {"title": "Ухоженные руки за один визит", "text": "Маникюр и педикюр в центре, работаем ежедневно", "hook": "", "angle": "identity", "why": "результат"},
]


def run(judge_answer):
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        asked.append((system, body["messages"][-1]["content"]))
        answer = json.dumps(CANDIDATES, ensure_ascii=False) if system == texts_ai.SYSTEM else judge_answer
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})

    out = asyncio.run(texts_ai.generate_texts(BIZ, 2, "", "m", "", httpx.MockTransport(handler), "fal-key", "model", "Запросы, по которым приходили обращения: маникюр центр"))
    return out, asked


def test_the_senior_specialist_picks_the_best_with_different_angles_and_hooks_are_cleaned():
    scores = json.dumps([{"i": 1, "score": 6, "why": "ок"}, {"i": 2, "score": 9, "why": "конкретно"}, {"i": 3, "score": 8, "why": "тот же угол, что у первого"},
                         {"i": 4, "score": 7, "why": "результат"}], ensure_ascii=False)
    out, asked = run(scores)
    assert [v["title"] for v in out] == ["Педикюр в центре города", "Маникюр и педикюр рядом"]   # лучшие 9 и 8: углы comfort и booking разные
    assert len({v["angle"] for v in out}) == 2
    assert out[0]["why"] == "конкретно" and out[0]["hook"] == "Педикюр ежедневно"             # пояснение судьи и короткий текст для баннера
    assert all(not check_hook(v["hook"]) for v in out)
    assert [s for s, _ in asked] == [texts_ai.SYSTEM, texts_ai.JUDGE_SYSTEM]                  # набор кандидатов, потом отбор
    assert "Запросы, по которым приходили обращения" in asked[0][1]                           # данные рекламы дошли до автора текстов


def test_a_hook_with_exclamation_is_dropped_and_a_broken_judge_keeps_the_first_candidates():
    out, _ = run("не json")
    assert [v["title"] for v in out][:1] == ["Маникюр в центре Москвы"] and len(out) == 2
    assert all(v["hook"] != "Салон в центре!" for v in out)                                   # «!» в тексте на баннере не допускается
    too_long = json.dumps([{"title": "Заголовок маникюр", "text": "Текст", "hook": "Слишком длинный текст для баннера, который не влезет в сорок знаков"}], ensure_ascii=False)
    assert texts_ai.parse_variants(too_long, "")[0]["hook"] == ""


def test_the_context_has_queries_with_leads_and_the_best_and_weak_ads(tmp_path):
    init_db(tmp_path / "t.db")
    conn = connect(tmp_path / "t.db")
    conn.execute("INSERT INTO users (id, email, password_hash) VALUES (1, 'a@b.c', 'x')")
    conn.execute("INSERT INTO businesses (id, user_id, name, kind) VALUES (1, 1, 'Лотос', 'Маникюрный салон')")
    conn.execute("INSERT INTO campaigns (id, business_id, name, source) VALUES (1, 1, 'к', 'direct')")
    today = date.today().isoformat()
    for uid, title, imp, clicks, leads in ((1, "Сильный заголовок", 1000, 60, 6), (2, "Слабый заголовок", 1000, 30, 0), (3, "Мало данных", 50, 2, 0)):
        conn.execute("INSERT INTO ad_units (id, campaign_id, business_id, title, created_day, angle) VALUES (?, 1, 1, ?, ?, 'booking')", (uid, title, today))
        conn.execute("INSERT INTO stats_daily (unit_id, day, impressions, clicks, spend_micro, leads) VALUES (?, ?, ?, ?, ?, ?)", (uid, day_add(today, -1), imp, clicks, 100 * MICRO, leads))
    conn.execute("INSERT INTO search_queries (business_id, query, impressions, clicks, spend_micro, leads) VALUES (1, 'маникюр центр', 100, 10, 1, 3)")
    conn.commit()
    biz = conn.execute("SELECT * FROM businesses WHERE id = 1").fetchone()
    text = texts_context(conn, biz, today)
    assert "маникюр центр" in text and "Сильный заголовок" in text.split("Лучшие объявления:")[1].split("Слабые")[0]
    assert "Слабый заголовок" in text.split("Слабые объявления")[1] and "Мало данных" not in text
    conn.close()


def test_strategist_overgenerates_and_the_judge_keeps_the_requested_number():
    names = ["альфа", "бета", "гамма", "дельта", "эпсилон", "зета"]
    ideas = [{"hypothesis": f"h{i}", "audience": f"a{i}", "angle": "booking", "title": f"Маникюр в центре {names[i - 1]}", "text": "Запишитесь онлайн", "hook": f"Маникюр {names[i - 1]}",
              "photo": None, "look": i % 5, "mood": "warm", "ref": None, "direction": "x"} for i in range(1, 7)]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        system = json.loads(request.content)["messages"][0]["content"]
        calls.append(system)
        if system == texts_ai.JUDGE_SYSTEM:
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps([{"i": k, "score": k} for k in range(1, 7)])}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(ideas, ensure_ascii=False)}}]})

    got = asyncio.run(plan_briefs(BIZ, [], 3, "key", "model", httpx.MockTransport(handler), "контекст"))
    assert len(got) == 3 and [b["title"] for b in got] == ["Маникюр в центре зета", "Маникюр в центре эпсилон", "Маникюр в центре дельта"]   # лучшие по оценке судьи
    assert all(b["hook"].startswith("Маникюр") for b in got) and len(calls) == 2
