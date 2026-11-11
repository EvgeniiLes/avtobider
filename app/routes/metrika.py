"""Подключение Яндекс Метрики: выбор счётчика и цели «обращение»."""
import asyncio
import sqlite3
from datetime import date
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.db import get_db
from app.deps import client_factory, current_user, yandex_http
from app.direct.client import DirectError
from app.direct.counter_health import check_counter, load_health
from app.direct.goals import save_goals
from app.direct.runner import account_token
from app.engine.util import EngineError
from app.routes.businesses import _own

router = APIRouter(prefix="/api/businesses/{business_id}/metrika", tags=["metrika"])
BASE = "https://api-metrika.yandex.net/management/v1"

HOW_TO = (
    "Метрика показывает сервису, сколько людей пришло с рекламы и сколько из них позвонили или написали. Без неё сервис следит только за бюджетом. "
    "Проще всего нажать «Создать счётчик автоматически»: сервис заведёт его в вашей Метрике и подготовит цели. "
    "Потом останется вставить номер счётчика в настройки вашего сайта (в конструкторах есть поле «Яндекс.Метрика»)."
)

# Цели, которые сервис заводит сам. Первые три Метрика определяет сама по счётчику, без кода на сайте; «lead» нужна только тем, у кого сайт делает разработчик.
AUTO_GOALS = [
    ("phone", "Клик по номеру телефона", {"type": "phone", "conditions": [{"type": "contain", "url": ""}]}),
    ("messenger", "Переход в мессенджер", {"type": "messenger", "conditions": [{"type": "contain", "url": ""}]}),
    ("email", "Клик по e-mail", {"type": "email", "conditions": [{"type": "contain", "url": ""}]}),
    ("lead", "Заявка: событие lead (для сайтов с разработчиком)", {"type": "action", "conditions": [{"type": "exact", "url": "lead"}]}),
]
MAIN_GOAL_ORDER = ("phone", "messenger", "email", "lead")   # что считать «обращением», если создалось несколько


def _token(conn, user, biz) -> str:
    account = biz["account_id"]
    if not account:
        row = conn.execute("SELECT id FROM ad_accounts WHERE user_id = ? AND platform = 'yandex_direct' ORDER BY id LIMIT 1", (user["id"],)).fetchone()
        account = row["id"] if row else None
    if not account:
        raise HTTPException(400, detail={"message": "Сначала подключите рекламный кабинет Яндекс Директа: через него сервис получает доступ и к Метрике."})
    try:
        return account_token(conn, account)[0]
    except EngineError as e:
        raise HTTPException(409, detail={"message": e.message}) from e


async def _get(http: httpx.AsyncClient, token: str, path: str) -> dict:
    try:
        r = await http.get(f"{BASE}{path}", headers={"Authorization": f"OAuth {token}"})
    except httpx.HTTPError as exc:
        raise HTTPException(502, detail={"message": "Не удалось связаться с Яндекс Метрикой. Попробуйте позже."}) from exc
    if r.status_code in (401, 403):
        raise HTTPException(409, detail={"message": "У сервиса нет доступа к Метрике. Подключите кабинет заново и разрешите доступ."})
    if r.status_code != 200:
        raise HTTPException(502, detail={"message": "Яндекс Метрика ответила ошибкой. Попробуйте позже."})
    return r.json()


@router.get("/counters")
def counters(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
             http: httpx.AsyncClient = Depends(yandex_http)):
    biz = _own(conn, user, business_id)
    token = _token(conn, user, biz)

    async def go():
        async with http:
            return await _get(http, token, "/counters?per_page=100")

    data = asyncio.run(go())
    items = [{"id": str(c["id"]), "name": c.get("name") or c.get("site") or str(c["id"]), "site": c.get("site", "")}
             for c in data.get("counters", [])]
    return {"counters": items, "selected": {"counter": biz["metrika_counter_id"], "goal": biz["metrika_goal_id"]}, "how_to": HOW_TO}


@router.get("/counters/{counter_id}/goals")
def goals(business_id: int, counter_id: str, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
          http: httpx.AsyncClient = Depends(yandex_http)):
    biz = _own(conn, user, business_id)
    token = _token(conn, user, biz)

    async def go():
        async with http:
            return await _get(http, token, f"/counter/{int(counter_id)}/goals")

    data = asyncio.run(go())
    return {"goals": [{"id": str(g["id"]), "name": g.get("name", str(g["id"]))} for g in data.get("goals", [])]}


async def _post(http: httpx.AsyncClient, token: str, path: str, body: dict) -> httpx.Response:
    try:
        return await http.post(f"{BASE}{path}", json=body, headers={"Authorization": f"OAuth {token}"})
    except httpx.HTTPError as exc:
        raise HTTPException(502, detail={"message": "Не удалось связаться с Яндекс Метрикой. Попробуйте позже."}) from exc


@router.post("/auto")
def create_automatically(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
                         http: httpx.AsyncClient = Depends(yandex_http)):
    """Сервис сам заводит счётчик на сайт бизнеса и типовые цели. Нужен доступ к Метрике на запись (даётся при подключении кабинета).
    Возвращает номер счётчика, номер цели «обращение» и что осталось сделать владельцу; выбор счётчика и цели сохраняет вызывающий (PUT)."""
    biz = _own(conn, user, business_id)
    token = _token(conn, user, biz)
    host = (urlsplit(biz["site_url"] if "://" in biz["site_url"] else f"https://{biz['site_url']}").hostname or "").lower()
    if not host:
        raise HTTPException(400, detail={"message": "Сначала укажите сайт на странице «Бизнес»: счётчик создаётся под адрес сайта."})
    bare = host[4:] if host.startswith("www.") else host

    async def go():
        async with http:
            made = await _post(http, token, "/counters", {"counter": {
                "name": f"{biz['name']}: сайт {bare}"[:255], "site2": {"site": bare}, "mirrors2": [{"site": f"www.{bare}"}],
                "code_options": {"clickmap": 1, "visor": 0}}})
            if made.status_code in (401, 403):
                raise HTTPException(409, detail={"message": "У сервиса нет права создавать счётчики в вашей Метрике. Подключите кабинет заново и разрешите доступ к Метрике на запись."})
            if made.status_code != 200 or not (made.json().get("counter") or {}).get("id"):
                raise HTTPException(502, detail={"message": "Яндекс Метрика не создала счётчик. Попробуйте позже или создайте его в Метрике сами и выберите ниже."})
            counter = int(made.json()["counter"]["id"])
            goals, skipped = {}, []
            for key, name, body in AUTO_GOALS:
                r = await _post(http, token, f"/counter/{counter}/goals", {"goal": {"name": name, **body}})
                goal_id = (r.json().get("goal") or {}).get("id") if r.status_code == 200 else None
                if goal_id:
                    goals[key] = {"id": str(goal_id), "name": name}
                else:
                    skipped.append(name)   # Метрика не приняла эту цель: остальное не страдает
            return counter, goals, skipped

    counter, goals, skipped = asyncio.run(go())
    main = next((goals[k] for k in MAIN_GOAL_ORDER if k in goals), None)
    return {"counter_id": str(counter), "goals": list(goals.values()), "skipped": skipped, "main_goal": main, "site": bare,
            "next": f"Вставьте номер счётчика {counter} в настройки сайта: в конструкторах (Tilda, Wix, Яндекс Бизнес, Рег.ру и др.) для этого есть поле «Яндекс.Метрика». "
                    "Если сайт делал разработчик, отправьте ему готовый код ниже. Через несколько минут после первых посещений нажмите «Проверить счётчик»."}


@router.post("/counters/{counter_id}/goals/auto")
def add_missing_goals(business_id: int, counter_id: str, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
                      http: httpx.AsyncClient = Depends(yandex_http)):
    """Дозаводит в уже существующем счётчике типовые цели, которых там нет (телефон, мессенджер, e-mail, заявка). Что есть, не трогает."""
    biz = _own(conn, user, business_id)
    if not counter_id.isdigit():
        raise HTTPException(400, detail={"message": "Выберите счётчик из списка."})
    token = _token(conn, user, biz)

    def has(existing: list[dict], name: str, body: dict) -> bool:
        for g in existing:
            if g.get("name") == name or g.get("type") == body["type"] and body["type"] != "action":
                return True
            if body["type"] == "action" and g.get("type") == "action" and any(c.get("url") == "lead" for c in g.get("conditions") or []):
                return True
        return False

    async def go():
        async with http:
            existing = (await _get(http, token, f"/counter/{counter_id}/goals")).get("goals", [])
            added, present, skipped, denied = [], [], [], False
            for key, name, body in AUTO_GOALS:
                if has(existing, name, body):
                    present.append(name)
                    continue
                r = await _post(http, token, f"/counter/{counter_id}/goals", {"goal": {"name": name, **body}})
                if r.status_code in (401, 403):
                    denied = True
                    break
                goal_id = (r.json().get("goal") or {}).get("id") if r.status_code == 200 else None
                if goal_id:
                    added.append(name)
                else:
                    skipped.append(name)
            if denied:
                raise HTTPException(409, detail={"message": "У сервиса нет права создавать цели в вашей Метрике. Подключите кабинет заново и разрешите доступ к Метрике на запись."})
            final = (await _get(http, token, f"/counter/{counter_id}/goals")).get("goals", [])
            return added, present, skipped, [{"id": str(g["id"]), "name": g.get("name") or str(g["id"])} for g in final]

    added, present, skipped, final = asyncio.run(go())
    if biz["metrika_counter_id"] == counter_id:
        save_goals(conn, business_id, final)
    return {"added": added, "present": present, "skipped": skipped, "goals": final}


@router.get("/health")
def health(business_id: int, refresh: bool = False, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
           http: httpx.AsyncClient = Depends(yandex_http)):
    """Работает ли счётчик: последний результат проверки, а с refresh=true проверка заново (у Метрики и по кликам рекламы)."""
    biz = _own(conn, user, business_id)
    if not biz["metrika_counter_id"]:
        return {"health": None}
    if not refresh:
        return {"health": load_health(biz)}
    token = _token(conn, user, biz)

    async def go():
        async with http:
            return await check_counter(conn, biz, token, date.today().isoformat(), http)

    return {"health": asyncio.run(go())}


TAG = """<!-- Яндекс Метрика -->
<script type="text/javascript">
(function(m,e,t,r,i,k,a){m[i]=m[i]||function(){(m[i].a=m[i].a||[]).push(arguments)};
m[i].l=1*new Date();
for (var j = 0; j < document.scripts.length; j++) {if (document.scripts[j].src === r) { return; }}
k=e.createElement(t),a=e.getElementsByTagName(t)[0],k.async=1,k.src=r,a.parentNode.insertBefore(k,a)})
(window, document, "script", "https://mc.yandex.ru/metrika/tag.js", "ym");
ym(%(id)s, "init", {clickmap:true, trackLinks:true, accurateTrackBounce:true, webvisor:true});
</script>
<noscript><div><img src="https://mc.yandex.ru/watch/%(id)s" style="position:absolute; left:-9999px;" alt="" /></div></noscript>
<!-- /Яндекс Метрика -->
<script>
// Цели для сервиса: звонок, Telegram, отправка формы (идентификаторы целей в Метрике: call, telegram, lead)
document.addEventListener("click", function (e) {
  var a = e.target.closest && e.target.closest("a[href]");
  if (!a) return;
  var href = a.getAttribute("href") || "";
  if (href.indexOf("tel:") === 0) ym(%(id)s, "reachGoal", "call");
  else if (/t\\.me\\/|telegram/i.test(href)) ym(%(id)s, "reachGoal", "telegram");
});
document.addEventListener("submit", function () { ym(%(id)s, "reachGoal", "lead"); });
</script>"""

SNIPPET_GOALS = [
    {"identifier": "lead", "title": "Заявка: отправка любой формы на сайте"},
    {"identifier": "call", "title": "Звонок: нажатие на номер телефона"},
    {"identifier": "telegram", "title": "Telegram: переход по ссылке на мессенджер"},
]


@router.get("/snippet")
def snippet(business_id: int, counter_id: str, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Готовый код для сайта: счётчик Метрики и отправка целей «заявка», «звонок», «Telegram». Токен Метрики не нужен: код зависит только от номера счётчика."""
    _own(conn, user, business_id)
    if not counter_id.isdigit():
        raise HTTPException(400, detail={"message": "Выберите счётчик из списка."})
    return {
        "counter": counter_id, "code": TAG % {"id": counter_id}, "goals": SNIPPET_GOALS,
        "steps": ["Вставьте код целиком перед закрывающим тегом </head> на всех страницах сайта (если счётчик уже стоит, оставьте только второй блок со скриптом целей).",
                  "В Метрике откройте «Настройки», «Цели» и создайте цели типа «JavaScript-событие» с идентификаторами из списка ниже.",
                  "Здесь выберите этот счётчик и цель «Заявка» (идентификатор lead) как обращение и нажмите «Сохранить»."],
    }


class MetrikaIn(BaseModel):
    counter_id: str
    goal_id: str


@router.put("")
def save(business_id: int, data: MetrikaIn, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
         http: httpx.AsyncClient = Depends(yandex_http), factory=Depends(client_factory)):
    biz = _own(conn, user, business_id)
    token = _token(conn, user, biz)
    if not (data.counter_id.isdigit() and data.goal_id.isdigit()):
        raise HTTPException(400, detail={"message": "Выберите счётчик и цель из списка."})

    async def check():
        async with http:
            found = await _get(http, token, f"/counter/{data.counter_id}/goals")
        if data.goal_id not in {str(g["id"]) for g in found.get("goals", [])}:
            raise HTTPException(400, detail={"message": "Такой цели нет в этом счётчике. Выберите цель из списка."})
        return [{"id": str(g["id"]), "name": g.get("name") or str(g["id"])} for g in found.get("goals", [])]

    all_goals = asyncio.run(check())
    save_goals(conn, business_id, all_goals)   # сервис сохраняет все цели счётчика, а не только выбранную «обращением»

    # Директ считает обращения только для кампаний, к которым привязан счётчик: привязываем к уже созданным.
    units = conn.execute("SELECT DISTINCT external_campaign_id FROM ad_units WHERE business_id = ? AND external_campaign_id IS NOT NULL", (business_id,)).fetchall()
    # кампания уже на «Максимум конверсий»: Яндекс оптимизирует под цель, которую ему назвали, поэтому новую цель называем и ему
    on_goal = conn.execute("SELECT * FROM campaigns WHERE business_id = ? AND external_id IS NOT NULL AND strategy = 'conversions'", (business_id,)).fetchone()
    if units:
        s = get_settings()

        async def attach():
            async with factory(token, api_version=s.direct_api_version) as client:
                for u in units:
                    await client.set_counters(u["external_campaign_id"], [int(data.counter_id)])
                if on_goal is not None and data.goal_id != (biz["metrika_goal_id"] or ""):
                    await client.set_strategy(on_goal["external_id"], "conversions", on_goal["weekly_micro"], data.goal_id)

        try:
            asyncio.run(attach())
        except DirectError as e:
            raise HTTPException(400, detail={"message": f"Не получилось привязать счётчик к кампаниям: {e.message}"}) from e

    conn.execute("UPDATE businesses SET metrika_counter_id = ?, metrika_goal_id = ?, counter_health = '' WHERE id = ?", (data.counter_id, data.goal_id, business_id))
    conn.commit()
    return {"ok": True}


@router.delete("")
def clear(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    _own(conn, user, business_id)
    conn.execute("UPDATE businesses SET metrika_counter_id = '', metrika_goal_id = '', counter_health = '' WHERE id = ?", (business_id,))
    conn.commit()
    return {"ok": True}
