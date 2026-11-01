"""Заявки с лендинга: форма на сайте клиента отправляет их сюда, сервис сохраняет и сразу пишет владельцу в Telegram.

Адрес приёма у каждого бизнеса свой, с секретным ключом (businesses.lead_key). Ключ лежит в коде страницы, поэтому он не пароль:
от спама защищают разрешённый адрес сайта (Origin), невидимое поле-ловушка, ограничение частоты и длины полей.
"""
import asyncio
import hashlib
import json
import logging
import secrets
import sqlite3
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import connect, get_db
from app.deps import current_user, telegram_transport
from app.notifier import make_sender
from app.routes.businesses import _own
from app import telegram

log = logging.getLogger("autobider.leads")

public = APIRouter(prefix="/api/leads", tags=["leads"])
owner = APIRouter(prefix="/api/businesses/{business_id}/leads", tags=["leads"])

MAX_BODY = 20_000
LIMITS = {"name": 100, "contact": 150, "task": 2000, "page": 300}
PER_10_MIN = 5       # заявок с одного адреса за десять минут
PER_DAY = 30


def _host(url: str) -> str:
    return (urlsplit(url if "://" in url else f"https://{url}").hostname or "").lower()


def origin_allowed(site_url: str, origin: str | None) -> bool:
    """Заявки принимаем только со страниц самого сайта (с www и без)."""
    site, got = _host(site_url), _host(origin or "")
    if not site or not got:
        return False
    return got == site or got == f"www.{site}" or f"www.{got}" == site


def _cors(response: Response, origin: str | None, allowed: bool) -> Response:
    if allowed and origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    return response


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def parse_body(raw: bytes, content_type: str) -> dict:
    if "json" in content_type:
        data = json.loads(raw.decode("utf-8") or "{}")
        return data if isinstance(data, dict) else {}
    return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()}


def lead_text(biz, lead: dict) -> str:
    lines = [f"Новая заявка с сайта «{biz['name']}»", ""]
    lines.append(f"Имя: {lead['name'] or 'не указано'}")
    lines.append(f"Контакт: {lead['contact']}")
    if lead["task"]:
        lines.append(f"Задача: {lead['task']}")
    if lead["page"]:
        lines.append(f"Страница: {lead['page']}")
    return "\n".join(lines)


def deliver(lead_id: int, send) -> None:
    """Отправляет заявку владельцу (в фоне, после ответа сайту). Не вышло, значит, заявка осталась в списке в сервисе, помечена «не доставлена»."""
    conn = connect(get_settings().db_path)
    try:
        lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (lead["business_id"],)).fetchone()
        chat = conn.execute("SELECT telegram_chat, notify_on FROM users WHERE id = ?", (biz["user_id"],)).fetchone()
        if send is None or not chat or not chat["telegram_chat"] or not chat["notify_on"]:
            return
        try:
            send(chat["telegram_chat"], lead_text(biz, dict(lead)))
        except telegram.TelegramError as e:
            log.warning("заявка %s: не удалось отправить в Telegram: %s", lead_id, e.message)
            return
        conn.execute("UPDATE leads SET delivered = 1 WHERE id = ?", (lead_id,))
        conn.commit()
    finally:
        conn.close()


def _ip_hash(request: Request) -> str:
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "")
    return hashlib.sha256(f"{ip}|{get_settings().secret_key}".encode()).hexdigest()[:24]


@public.options("/{key}")
def preflight(key: str, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    biz = conn.execute("SELECT site_url FROM businesses WHERE lead_key = ? AND lead_key != ''", (key,)).fetchone()
    origin = request.headers.get("origin")
    response = Response(status_code=204)
    if biz and origin_allowed(biz["site_url"], origin):
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Max-Age"] = "600"
    return _cors(response, origin, bool(biz) and origin_allowed(biz["site_url"], origin))


@public.post("/{key}")
async def receive(key: str, request: Request, background: BackgroundTasks, conn: sqlite3.Connection = Depends(get_db),
                  transport=Depends(telegram_transport)):
    biz = conn.execute("SELECT * FROM businesses WHERE lead_key = ? AND lead_key != ''", (key,)).fetchone()
    if biz is None:
        return JSONResponse({"ok": False, "message": "Адрес приёма заявок не найден."}, status_code=404)
    origin = request.headers.get("origin")
    if origin and not origin_allowed(biz["site_url"], origin):
        return JSONResponse({"ok": False, "message": "Заявки принимаются только со страниц сайта."}, status_code=403)
    reply = lambda body, status=200: _cors(JSONResponse(body, status_code=status), origin, True)
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return reply({"ok": False, "message": "Слишком длинная заявка."}, 413)
    try:
        data = parse_body(raw, request.headers.get("content-type", ""))
    except (ValueError, UnicodeDecodeError):
        return reply({"ok": False, "message": "Не удалось прочитать заявку."}, 400)
    if _clean(data.get("website")):   # невидимое поле для роботов: человек его не заполняет
        return reply({"ok": True})
    lead = {k: _clean(data.get(k))[:limit] for k, limit in LIMITS.items()}
    if not lead["contact"]:
        return reply({"ok": False, "message": "Укажите, как с вами связаться."}, 422)
    who = _ip_hash(request)
    recent = conn.execute("SELECT COUNT(*) FROM leads WHERE business_id = ? AND ip_hash = ? AND created_at >= datetime('now', '-10 minutes')", (biz["id"], who)).fetchone()[0]
    today = conn.execute("SELECT COUNT(*) FROM leads WHERE business_id = ? AND ip_hash = ? AND created_at >= datetime('now', '-1 day')", (biz["id"], who)).fetchone()[0]
    if recent >= PER_10_MIN or today >= PER_DAY:
        return reply({"ok": False, "message": "Слишком много заявок подряд. Попробуйте позже или напишите нам в мессенджер."}, 429)
    cur = conn.execute("INSERT INTO leads (business_id, name, contact, task, page, ip_hash) VALUES (?,?,?,?,?,?)",
                       (biz["id"], lead["name"], lead["contact"], lead["task"], lead["page"], who))
    conn.commit()
    background.add_task(deliver, cur.lastrowid, make_sender(transport))
    return reply({"ok": True})


# ---------- владелец: ключ, код для сайта, список заявок ----------

def _ensure_key(conn: sqlite3.Connection, biz) -> str:
    if biz["lead_key"]:
        return biz["lead_key"]
    key = secrets.token_urlsafe(18)
    conn.execute("UPDATE businesses SET lead_key = ? WHERE id = ?", (key, biz["id"]))
    conn.commit()
    return key


def snippet(endpoint: str) -> str:
    return (
        "// В обработчике отправки формы вместо «form.classList.add('sent'); goal('form_sent');»:\n"
        "const data = new URLSearchParams(new FormData(form)); data.set('page', location.href);\n"
        f"fetch('{endpoint}', {{ method: 'POST', body: data }})\n"
        "  .then(r => { if (!r.ok) throw new Error(r.status); form.classList.add('sent'); goal('form_sent'); })\n"
        "  .catch(() => alert('Не удалось отправить заявку, напишите нам в Telegram.'));\n"
        "// В форме должны быть поля name, contact, task. Скрытое поле-ловушка: <input name=\"website\" tabindex=\"-1\" autocomplete=\"off\" style=\"position:absolute;left:-9999px\">"
    )


def _view(request: Request, conn: sqlite3.Connection, biz) -> dict:
    key = _ensure_key(conn, biz)
    proto = request.headers.get("x-forwarded-proto", "https")
    endpoint = f"{proto}://{request.headers.get('host', 'addtracker.ru')}/api/leads/{key}"
    owner_row = conn.execute("SELECT telegram_chat, notify_on FROM users WHERE id = ?", (biz["user_id"],)).fetchone()
    leads = [dict(r) for r in conn.execute(
        "SELECT id, name, contact, task, page, delivered, created_at FROM leads WHERE business_id = ? ORDER BY id DESC LIMIT 50", (biz["id"],))]
    return {"endpoint": endpoint, "snippet": snippet(endpoint), "leads": leads,
            "telegram": bool(owner_row["telegram_chat"] and owner_row["notify_on"]), "site": biz["site_url"]}


@owner.get("")
def get_leads(business_id: int, request: Request, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    return _view(request, conn, _own(conn, user, business_id))


@owner.post("/key")
def new_key(business_id: int, request: Request, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Новый ключ: прежний адрес приёма перестаёт работать (если ключ попал к спамерам)."""
    biz = _own(conn, user, business_id)
    conn.execute("UPDATE businesses SET lead_key = '' WHERE id = ?", (biz["id"],))
    conn.commit()
    return _view(request, conn, conn.execute("SELECT * FROM businesses WHERE id = ?", (biz["id"],)).fetchone())
