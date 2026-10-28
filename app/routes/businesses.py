"""Описание бизнеса, его файлы (логотип, фото, видео) и настройки."""
import re
import secrets
import shutil
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from app.assets import (
    IMAGE_MAX_BYTES,
    KINDS,
    LIMITS,
    PHOTO_TAGS,
    VIDEO_MAX_BYTES,
    AssetError,
    prepare_image,
    prepare_video,
)
from app.config import get_settings
from app.db import get_db
from app.deps import current_user

router = APIRouter(prefix="/api/businesses", tags=["businesses"])

MICRO = 1_000_000
MAX_BUSINESSES = 5
TONES = {
    "gentle": "Нежный",
    "calm": "Спокойный",
    "energetic": "Энергичный",
    "strict": "Строгий",
    "friendly": "Дружелюбный",
}
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# Ссылка на организацию в Яндекс Картах: yandex.ru/maps/..., yandex.ru/profile/..., короткие yandex.ru/maps/-/XXXX
MAPS_RE = re.compile(r"^https?://(www\.)?yandex\.(ru|com|kz|by|uz)/(maps|profile)/", re.I)
MAPS_ORG_RE = re.compile(r"/(?:org/[^/]+|profile)/(\d{5,})", re.I)


def maps_org_id(url: str) -> str:
    """Номер организации из ссылки на Яндекс Карты (если он в ней есть)."""
    m = MAPS_ORG_RE.search(url or "")
    return m.group(1) if m else ""


class BusinessIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=2000)
    city: str = Field(default="", max_length=100)
    site_url: str = Field(default="", max_length=300)
    maps_url: str = Field(default="", max_length=500)
    tone: str = ""
    color_main: str = ""
    color_extra: str = ""
    avg_check: float = Field(default=0, ge=0, le=100_000_000)
    target_cpa: float = Field(default=0, ge=0, le=100_000_000)
    weekly_budget: float = Field(default=0, ge=0, le=100_000_000)
    client_share_percent: float = Field(default=30, ge=1, le=100)  # какая часть обращений становится клиентами
    mode: Literal["ask", "auto"] = "ask"  # ask: сначала спрашивать, auto: делать самому

    @field_validator("maps_url")
    @classmethod
    def _maps(cls, v: str) -> str:
        v = v.strip()
        if v and not re.match(r"^https?://", v, re.I):
            v = "https://" + v
        if v and not MAPS_RE.match(v):
            raise ValueError("Нужна ссылка на вашу организацию в Яндекс Картах, например https://yandex.ru/maps/org/...")
        return v

    @field_validator("name", "kind", "description", "city", "site_url")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("tone")
    @classmethod
    def _tone(cls, v: str) -> str:
        if v and v not in TONES:
            raise ValueError("Неизвестный стиль")
        return v

    @field_validator("color_main", "color_extra")
    @classmethod
    def _color(cls, v: str) -> str:
        if v and not COLOR_RE.match(v):
            raise ValueError("Цвет должен быть вида #aabbcc")
        return v.lower()

    @field_validator("site_url")
    @classmethod
    def _url(cls, v: str) -> str:
        if v and not re.match(r"^https?://", v, re.I):
            v = "https://" + v
        return v


def _rub(micro: int) -> float:
    return round(micro / MICRO, 2)


def _micro(rub: float) -> int:
    return int(round(rub * MICRO))


def _asset(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "tag": row["tag"],
        "tag_label": PHOTO_TAGS.get(row["tag"], ""),
        "caption": row["caption"],
        "name": row["orig_name"],
        "mime": row["mime"],
        "size_bytes": row["size_bytes"],
        "width": row["width"],
        "height": row["height"],
        "url": f"/api/businesses/{row['business_id']}/assets/{row['id']}/file",
    }


def _business(row: sqlite3.Row, assets: list[sqlite3.Row] | None = None) -> dict:
    data = {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "description": row["description"],
        "city": row["city"],
        "site_url": row["site_url"],
        "maps_url": row["maps_url"],
        "maps_org_id": maps_org_id(row["maps_url"]),
        "tone": row["tone"],
        "color_main": row["color_main"],
        "color_extra": row["color_extra"],
        "avg_check": _rub(row["avg_check_micro"]),
        "target_cpa": _rub(row["target_cpa_micro"]),
        "weekly_budget": _rub(row["weekly_budget_micro"]),
        "client_share_percent": round(row["client_share"] * 100, 1),
        "mode": row["mode"],
        "status": row["status"],
    }
    if assets is not None:
        data["assets"] = [_asset(a) for a in assets]
    return data


def _own(conn: sqlite3.Connection, user: sqlite3.Row, business_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM businesses WHERE id = ? AND user_id = ?", (business_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(404, detail={"message": "Такой бизнес не найден."})
    return row


def _assets_of(conn: sqlite3.Connection, business_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM business_assets WHERE business_id = ? ORDER BY id", (business_id,)).fetchall()


def _asset_path(business_id: int, stored_name: str) -> Path:
    return get_settings().uploads_dir / str(business_id) / stored_name


@router.get("/options")
def options():
    """Подписи для выпадающих списков на странице."""
    return {
        "tones": [{"value": k, "label": v} for k, v in TONES.items()],
        "photo_tags": [{"value": k, "label": v} for k, v in PHOTO_TAGS.items()],
        "limits": LIMITS,
    }


@router.get("")
def list_businesses(user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute("SELECT * FROM businesses WHERE user_id = ? ORDER BY id", (user["id"],)).fetchall()
    return {"businesses": [_business(r) for r in rows]}


@router.post("")
def create_business(data: BusinessIn, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    count = conn.execute("SELECT COUNT(*) FROM businesses WHERE user_id = ?", (user["id"],)).fetchone()[0]
    if count >= MAX_BUSINESSES:
        raise HTTPException(400, detail={"message": f"Можно добавить не больше {MAX_BUSINESSES} бизнесов."})
    cur = conn.execute(
        "INSERT INTO businesses (user_id, name, kind, description, city, site_url, maps_url, tone, color_main, color_extra, "
        "avg_check_micro, target_cpa_micro, weekly_budget_micro, client_share, mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            user["id"], data.name, data.kind, data.description, data.city, data.site_url, data.maps_url, data.tone,
            data.color_main, data.color_extra, _micro(data.avg_check), _micro(data.target_cpa), _micro(data.weekly_budget),
            data.client_share_percent / 100, data.mode,
        ),
    )
    conn.commit()
    return _business(_own(conn, user, cur.lastrowid), [])


@router.get("/{business_id}")
def get_business(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    row = _own(conn, user, business_id)
    return _business(row, _assets_of(conn, business_id))


@router.put("/{business_id}")
def update_business(
    business_id: int,
    data: BusinessIn,
    user: sqlite3.Row = Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    _own(conn, user, business_id)
    conn.execute(
        "UPDATE businesses SET name=?, kind=?, description=?, city=?, site_url=?, maps_url=?, tone=?, color_main=?, color_extra=?, "
        "avg_check_micro=?, target_cpa_micro=?, weekly_budget_micro=?, client_share=?, mode=? WHERE id=?",
        (
            data.name, data.kind, data.description, data.city, data.site_url, data.maps_url, data.tone, data.color_main,
            data.color_extra, _micro(data.avg_check), _micro(data.target_cpa), _micro(data.weekly_budget),
            data.client_share_percent / 100, data.mode, business_id,
        ),
    )
    conn.commit()
    return _business(_own(conn, user, business_id), _assets_of(conn, business_id))


@router.delete("/{business_id}")
def delete_business(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    _own(conn, user, business_id)
    conn.execute("DELETE FROM businesses WHERE id = ?", (business_id,))
    conn.commit()
    shutil.rmtree(get_settings().uploads_dir / str(business_id), ignore_errors=True)
    return {"ok": True}


# ---------- файлы ----------

async def _read_limited(file: UploadFile, limit: int) -> bytes:
    """Читает файл кусками и останавливается, если он больше лимита."""
    chunks, total = [], 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > limit:
            raise AssetError("Файл слишком большой.")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/{business_id}/assets")
async def upload_asset(
    business_id: int,
    kind: str = Form(...),
    tag: str = Form(""),
    caption: str = Form(""),
    file: UploadFile = File(...),
    user: sqlite3.Row = Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    _own(conn, user, business_id)
    if kind not in KINDS:
        raise HTTPException(400, detail={"message": "Неизвестный тип файла."})
    if kind == "photo" and tag not in PHOTO_TAGS:
        tag = "other"
    if kind != "photo":
        tag = ""

    existing = conn.execute(
        "SELECT COUNT(*) FROM business_assets WHERE business_id = ? AND kind = ?", (business_id, kind)
    ).fetchone()[0]
    if kind != "logo" and existing >= LIMITS[kind]:
        raise HTTPException(400, detail={"message": f"Достигнут предел: не больше {LIMITS[kind]} файлов этого типа."})

    try:
        data = await _read_limited(file, VIDEO_MAX_BYTES if kind == "video" else IMAGE_MAX_BYTES)
        prepared = prepare_video(data) if kind == "video" else prepare_image(data, kind)
    except AssetError as e:
        raise HTTPException(400, detail={"message": str(e)})

    stored_name = f"{secrets.token_hex(16)}.{prepared.ext}"
    path = _asset_path(business_id, stored_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(prepared.data)

    # Логотип один: новый заменяет старый.
    if kind == "logo":
        for old in conn.execute("SELECT * FROM business_assets WHERE business_id = ? AND kind = 'logo'", (business_id,)).fetchall():
            _asset_path(business_id, old["stored_name"]).unlink(missing_ok=True)
            conn.execute("DELETE FROM business_assets WHERE id = ?", (old["id"],))

    cur = conn.execute(
        "INSERT INTO business_assets (business_id, kind, tag, caption, stored_name, orig_name, mime, size_bytes, width, height) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            business_id, kind, tag, caption.strip()[:200], stored_name, (file.filename or "")[:200],
            prepared.mime, len(prepared.data), prepared.width, prepared.height,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM business_assets WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _asset(row)


def _own_asset(conn: sqlite3.Connection, user: sqlite3.Row, business_id: int, asset_id: int) -> sqlite3.Row:
    _own(conn, user, business_id)
    row = conn.execute(
        "SELECT * FROM business_assets WHERE id = ? AND business_id = ?", (asset_id, business_id)
    ).fetchone()
    if row is None:
        raise HTTPException(404, detail={"message": "Такой файл не найден."})
    return row


@router.get("/{business_id}/assets/{asset_id}/file")
def asset_file(
    business_id: int,
    asset_id: int,
    user: sqlite3.Row = Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    row = _own_asset(conn, user, business_id, asset_id)
    path = _asset_path(business_id, row["stored_name"])
    if not path.is_file():
        raise HTTPException(404, detail={"message": "Файл потерян. Загрузите его заново."})
    return FileResponse(
        path,
        media_type=row["mime"],
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"},
    )


@router.delete("/{business_id}/assets/{asset_id}")
def delete_asset(
    business_id: int,
    asset_id: int,
    user: sqlite3.Row = Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    row = _own_asset(conn, user, business_id, asset_id)
    _asset_path(business_id, row["stored_name"]).unlink(missing_ok=True)
    conn.execute("DELETE FROM business_assets WHERE id = ?", (asset_id,))
    conn.commit()
    return {"ok": True}
