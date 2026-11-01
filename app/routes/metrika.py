"""Подключение Яндекс Метрики: выбор счётчика и цели «обращение»."""
import asyncio
import sqlite3

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.db import get_db
from app.deps import client_factory, current_user, yandex_http
from app.direct.client import DirectError
from app.direct.goals import save_goals
from app.direct.runner import account_token
from app.engine.util import EngineError
from app.routes.businesses import _own

router = APIRouter(prefix="/api/businesses/{business_id}/metrika", tags=["metrika"])
BASE = "https://api-metrika.yandex.net/management/v1"

HOW_TO = (
    "Чтобы сервис видел обращения, на вашем сайте должен стоять счётчик Яндекс Метрики и цель «Обращение». "
    "Создайте цель в Метрике типа «JavaScript-событие» с идентификатором lead и вызывайте "
    "ym(НОМЕР_СЧЁТЧИКА, 'reachGoal', 'lead') при заявке или звонке. Потом выберите счётчик и цель здесь."
)


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
        raise HTTPException(409, detail={"message": e.message})


async def _get(http: httpx.AsyncClient, token: str, path: str) -> dict:
    try:
        r = await http.get(f"{BASE}{path}", headers={"Authorization": f"OAuth {token}"})
    except httpx.HTTPError:
        raise HTTPException(502, detail={"message": "Не удалось связаться с Яндекс Метрикой. Попробуйте позже."})
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
            raise HTTPException(400, detail={"message": f"Не получилось привязать счётчик к кампаниям: {e.message}"})

    conn.execute("UPDATE businesses SET metrika_counter_id = ?, metrika_goal_id = ? WHERE id = ?", (data.counter_id, data.goal_id, business_id))
    conn.commit()
    return {"ok": True}


@router.delete("")
def clear(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    _own(conn, user, business_id)
    conn.execute("UPDATE businesses SET metrika_counter_id = '', metrika_goal_id = '' WHERE id = ?", (business_id,))
    conn.commit()
    return {"ok": True}
