"""Креативы: тексты, картинки и видео для объявлений."""
import asyncio
import base64
import json
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.assets import AssetError, prepare_image, prepare_video
from app.config import get_settings
from app.creative import fal
from app.creative.texts_ai import generate_texts
from app.db import get_db
from app.deps import creative_transport, current_user
from app.direct.texts import TEXT_MAX, TITLE_MAX, check, draft_variants
from app.routes.businesses import _asset_path, _own

import secrets

router = APIRouter(prefix="/api/businesses/{business_id}/creatives", tags=["creatives"])

STATUSES = ("draft", "approved", "rejected")
MAX_TEXTS = 30


def _spent_cents(conn, business_id: int) -> int:
    return conn.execute("SELECT COALESCE(SUM(cents),0) FROM generations WHERE business_id = ?", (business_id,)).fetchone()[0]


def _charge(conn, business_id: int, kind: str) -> None:
    """Записывает расход на генерацию. Выше лимита генерировать не даём."""
    limit = round(get_settings().gen_limit_usd * 100)
    cents = fal.COST_CENTS[kind]
    if _spent_cents(conn, business_id) + cents > limit:
        raise HTTPException(400, detail={"message": f"Лимит расходов на генерацию исчерпан (${limit / 100:.0f}). Администратор может увеличить его настройкой GEN_LIMIT_USD."})
    conn.execute("INSERT INTO generations (business_id, kind, cents) VALUES (?,?,?)", (business_id, kind, cents))


def _caps() -> dict:
    s = get_settings()
    return {"text_ai": bool(s.anthropic_api_key or s.fal_key), "image_ai": bool(s.fal_key), "video_ai": bool(s.fal_key)}


def _row(r: sqlite3.Row, business_id: int) -> dict:
    return {
        "id": r["id"], "kind": r["kind"], "title": r["title"], "body": r["body"], "status": r["status"], "source": r["source"],
        "error": r["error"], "prompt": r["prompt"],
        "url": f"/api/businesses/{business_id}/assets/{r['asset_id']}/file" if r["asset_id"] else "",
        "created_at": r["created_at"],
    }


def _own_creative(conn, business_id: int, creative_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM creatives WHERE id = ? AND business_id = ?", (creative_id, business_id)).fetchone()
    if row is None:
        raise HTTPException(404, detail={"message": "Такой креатив не найден."})
    return row


def _save_asset(conn, business_id: int, kind: str, data: bytes, name: str) -> int:
    """Сохраняет сгенерированный файл так же, как загруженный пользователем."""
    prepared = prepare_video(data) if kind == "video" else prepare_image(data, "photo")
    stored = f"{secrets.token_hex(16)}.{prepared.ext}"
    path = _asset_path(business_id, stored)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(prepared.data)
    cur = conn.execute(
        "INSERT INTO business_assets (business_id, kind, tag, caption, stored_name, orig_name, mime, size_bytes, width, height) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (business_id, "generated" if kind == "image" else "generated_video", "", "Создано сервисом", stored, name,
         prepared.mime, len(prepared.data), prepared.width, prepared.height),
    )
    return cur.lastrowid


def _refresh_working(conn, business_id: int, transport) -> None:
    """Забирает готовые картинки и видео у генератора. Вызывается при каждом открытии списка."""
    key = get_settings().fal_key
    for r in conn.execute("SELECT * FROM creatives WHERE business_id = ? AND status = 'working'", (business_id,)).fetchall():
        if not key or not r["external_id"]:
            continue
        state, value = asyncio.run(fal.poll(json.loads(r["external_id"]), key, transport))
        if state == "working":
            continue
        if state == "failed":
            conn.execute("UPDATE creatives SET status = 'failed', error = ? WHERE id = ?", (value, r["id"]))
            continue
        try:
            data = asyncio.run(fal.download(value, transport))
            asset_id = _save_asset(conn, business_id, r["kind"], data, f"creative-{r['id']}")
            conn.execute("UPDATE creatives SET status = 'draft', asset_id = ?, external_id = '' WHERE id = ?", (asset_id, r["id"]))
        except (fal.FalError, AssetError) as e:
            conn.execute("UPDATE creatives SET status = 'failed', error = ? WHERE id = ?", (e.message if hasattr(e, "message") else str(e), r["id"]))
    conn.commit()


@router.get("")
def list_creatives(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
                   transport=Depends(creative_transport)):
    _own(conn, user, business_id)
    _refresh_working(conn, business_id, transport)
    rows = conn.execute("SELECT * FROM creatives WHERE business_id = ? ORDER BY id DESC", (business_id,)).fetchall()
    limit = get_settings().gen_limit_usd
    return {
        "creatives": [_row(r, business_id) for r in rows],
        "caps": _caps(),
        "spend": {"spent_usd": round(_spent_cents(conn, business_id) / 100, 2), "limit_usd": limit},
        "limits": {"title": TITLE_MAX, "text": TEXT_MAX},
        "prices_usd": {"image": fal.COST_CENTS["image"] / 100, "video": fal.COST_CENTS["video"] / 100},
    }


class TextsIn(BaseModel):
    count: int = Field(default=6, ge=1, le=10)
    hint: str = Field(default="", max_length=300)


@router.post("/texts/generate")
def generate(business_id: int, data: TextsIn, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
             transport=Depends(creative_transport)):
    biz = _own(conn, user, business_id)
    if conn.execute("SELECT COUNT(*) FROM creatives WHERE business_id = ? AND kind = 'text'", (business_id,)).fetchone()[0] >= MAX_TEXTS:
        raise HTTPException(400, detail={"message": f"Уже накопилось {MAX_TEXTS} вариантов. Удалите ненужные, и придумаем новые."})
    s = get_settings()
    existing = {r["title"].lower() for r in conn.execute("SELECT title FROM creatives WHERE business_id = ? AND kind = 'text'", (business_id,))}
    made: list[tuple[dict, str]] = []
    if s.anthropic_api_key or s.fal_key:
        _charge(conn, business_id, "text")
        for v in asyncio.run(generate_texts(biz, data.count, s.anthropic_api_key, s.anthropic_model, data.hint, transport,
                                            s.fal_key, s.fal_text_model)):
            if v["title"].lower() not in existing:
                made.append((v, "claude"))
    ai_count = len(made)
    for v in draft_variants(biz, 6):  # запасные варианты по шаблону: добираем, если ИИ нет или он дал меньше
        if len(made) >= data.count:
            break
        if v["title"].lower() not in existing and v["title"].lower() not in {m[0]["title"].lower() for m in made}:
            made.append((v, "template"))
    added = 0
    for v, source in made[:data.count]:
        existing.add(v["title"].lower())
        conn.execute("INSERT INTO creatives (business_id, kind, title, body, source) VALUES (?, 'text', ?, ?, ?)",
                     (business_id, v["title"], v["text"], source))
        added += 1
    conn.commit()
    has_ai = bool(s.anthropic_api_key or s.fal_key)
    return {"added": added, "ai": ai_count, "used_ai": has_ai,
            "message": ("Придумали варианты с помощью ИИ." if ai_count else
                        "Варианты собраны по шаблону: ИИ для текстов пока не подключён." if not has_ai else
                        "ИИ не ответил, поэтому варианты собраны по шаблону. Попробуйте ещё раз позже.") if added else
                       "Новых вариантов не нашлось: такие заголовки уже есть."}


class TextIn(BaseModel):
    title: str = Field(max_length=200)
    body: str = Field(max_length=400)


@router.post("/texts")
def add_text(business_id: int, data: TextIn, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    _own(conn, user, business_id)
    problems = check(data.title.strip(), data.body.strip())
    if problems:
        raise HTTPException(400, detail={"message": " ".join(problems)})
    cur = conn.execute("INSERT INTO creatives (business_id, kind, title, body, source, status) VALUES (?, 'text', ?, ?, 'user', 'draft')",
                       (business_id, data.title.strip(), data.body.strip()))
    conn.commit()
    return _row(conn.execute("SELECT * FROM creatives WHERE id = ?", (cur.lastrowid,)).fetchone(), business_id)


class CreativeIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=400)
    status: Literal["draft", "approved", "rejected"] | None = None


@router.put("/{creative_id}")
def update(business_id: int, creative_id: int, data: CreativeIn, user: sqlite3.Row = Depends(current_user),
           conn: sqlite3.Connection = Depends(get_db)):
    _own(conn, user, business_id)
    row = _own_creative(conn, business_id, creative_id)
    title = row["title"] if data.title is None else data.title.strip()
    body = row["body"] if data.body is None else data.body.strip()
    if row["kind"] == "text":
        problems = check(title, body)
        if problems and (data.title is not None or data.body is not None or data.status == "approved"):
            raise HTTPException(400, detail={"message": " ".join(problems)})
    elif row["status"] in ("working", "failed") and data.status == "approved":
        raise HTTPException(400, detail={"message": "Этот файл ещё не готов."})
    conn.execute("UPDATE creatives SET title = ?, body = ?, status = ? WHERE id = ?",
                 (title, body, data.status or row["status"], creative_id))
    conn.commit()
    return _row(conn.execute("SELECT * FROM creatives WHERE id = ?", (creative_id,)).fetchone(), business_id)


@router.delete("/{creative_id}")
def delete(business_id: int, creative_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    _own(conn, user, business_id)
    row = _own_creative(conn, business_id, creative_id)
    if row["asset_id"]:
        asset = conn.execute("SELECT * FROM business_assets WHERE id = ?", (row["asset_id"],)).fetchone()
        if asset:
            _asset_path(business_id, asset["stored_name"]).unlink(missing_ok=True)
            conn.execute("DELETE FROM business_assets WHERE id = ?", (asset["id"],))
    conn.execute("DELETE FROM creatives WHERE id = ?", (creative_id,))
    conn.commit()
    return {"ok": True}


class ImagesIn(BaseModel):
    count: int = Field(default=2, ge=1, le=3)
    idea: str = Field(default="", max_length=300)


def _need_fal() -> str:
    key = get_settings().fal_key
    if not key:
        raise HTTPException(503, detail={"message": "Создание картинок и видео пока не подключено на сервере: нужен ключ сервиса генерации."})
    return key


@router.post("/images/generate")
def generate_images(business_id: int, data: ImagesIn, user: sqlite3.Row = Depends(current_user),
                    conn: sqlite3.Connection = Depends(get_db), transport=Depends(creative_transport)):
    biz = _own(conn, user, business_id)
    key = _need_fal()
    s = get_settings()
    prompt = fal.image_prompt(biz, data.idea)
    for _ in range(data.count):
        _charge(conn, business_id, "image")
        try:
            job = asyncio.run(fal.start_image(prompt, key, s.fal_image_model, transport))
        except fal.FalError as e:
            conn.commit()
            raise HTTPException(502, detail={"message": e.message})
        conn.execute("INSERT INTO creatives (business_id, kind, prompt, status, source, external_id) VALUES (?, 'image', ?, 'working', 'fal', ?)",
                     (business_id, prompt, json.dumps(job)))
    conn.commit()
    return {"started": data.count}


class VideoIn(BaseModel):
    image_creative_id: int


@router.post("/videos/generate")
def generate_video(business_id: int, data: VideoIn, user: sqlite3.Row = Depends(current_user),
                   conn: sqlite3.Connection = Depends(get_db), transport=Depends(creative_transport)):
    _own(conn, user, business_id)
    key = _need_fal()
    image = _own_creative(conn, business_id, data.image_creative_id)
    if image["kind"] != "image" or not image["asset_id"]:
        raise HTTPException(400, detail={"message": "Для видео нужна готовая картинка."})
    asset = conn.execute("SELECT * FROM business_assets WHERE id = ?", (image["asset_id"],)).fetchone()
    raw = _asset_path(business_id, asset["stored_name"]).read_bytes()
    data_uri = f"data:{asset['mime']};base64,{base64.b64encode(raw).decode()}"
    _charge(conn, business_id, "video")
    try:
        job = asyncio.run(fal.start_video(data_uri, "Плавное естественное движение камеры, без текста и надписей.", key,
                                          get_settings().fal_video_model, transport))
    except fal.FalError as e:
        conn.commit()
        raise HTTPException(502, detail={"message": e.message})
    conn.execute("INSERT INTO creatives (business_id, kind, prompt, status, source, external_id) VALUES (?, 'video', ?, 'working', 'fal', ?)",
                 (business_id, f"из картинки #{image['id']}", json.dumps(job)))
    conn.commit()
    return {"started": 1}
