"""Креативы: тексты, картинки и видео для объявлений."""
import asyncio
import base64
import hashlib
import json
import sqlite3
from io import BytesIO
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from PIL import Image
from pydantic import BaseModel, Field

from app.assets import AssetError, prepare_image, prepare_video
from app.config import get_settings
from app.creative import fal
from app.creative.angles import label as angle_label, profile
from app.assets import PHOTO_TAGS
from app.creative.compose import add_logo, find_price, prepare_for_direct, style_for
from app.creative.art import art_direction, review_banner, scene_label
from app.creative.screen import insert_logo, insert_screenshot
from app.creative.texts_ai import check_photo, generate_texts, missing_phrases, read_image_text
from app.db import get_db
from app.deps import creative_transport, current_user
from app.direct.launch import banner_layout, choose_variants, logo_for, media_typed, photo_for, render_banner, start_split, text_candidates
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
        "angle": r["angle"], "angle_label": angle_label(r["angle"]),
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


def _bytes_url(data: bytes) -> str:
    """Картинка в виде data-адреса для модели со зрением (уменьшенная: быстрее и дешевле)."""
    img = Image.open(BytesIO(data)).convert("RGB")
    img.thumbnail((1024, 1024))
    buf = BytesIO()
    img.save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


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
        if r["kind"] == "ai_banner":  # нейросеть могла ошибиться в буквах: сверяем надписи с нужными, ошибочную не принимаем
            expected = json.loads(r["prompt"] or "{}").get("expect", [])
            s = get_settings()
            read = asyncio.run(read_image_text(value, key, s.fal_text_model, transport))
            wrong = missing_phrases(expected, read) if read is not None else []
            if wrong:
                conn.execute("UPDATE creatives SET status = 'failed', error = ? WHERE id = ?",
                             (f"На картинке ошибки в тексте: {', '.join(wrong)}. Нажмите «Нарисовать» ещё раз.", r["id"]))
                continue
        try:
            data = asyncio.run(fal.download(value, transport))
            if r["kind"] == "ai_banner":
                shot = _screenshot(conn, business_id)
                if shot:  # настоящий скриншот ложится в пурпурный экран телефона
                    composed = insert_screenshot(data, shot)
                    if composed is None:
                        conn.execute("UPDATE creatives SET status = 'failed', error = ? WHERE id = ?",
                                     ("Не нашли на картинке экран телефона для скриншота. Нажмите «Другой баннер» ещё раз.", r["id"]))
                        continue
                    data = composed
            meta = json.loads(r["prompt"] or "{}") if r["kind"] == "ai_banner" else {}
            if meta.get("logo"):  # логотип вшиваем в готовую картинку: в слот, который ИИ оставил под него, а нет слота — в спокойный угол
                biz_row = conn.execute("SELECT * FROM businesses WHERE id = ?", (business_id,)).fetchone()
                mark = logo_for(conn, biz_row) if biz_row else None
                if mark:
                    data = insert_logo(data, mark) or add_logo(data, mark)
                    meta["baked"] = True
            if r["kind"] == "ai_banner" and meta.get("brief"):  # самопроверка итоговой картинки: слабую не принимаем, чтобы не платить за неё рекламой
                verdict = asyncio.run(review_banner(_bytes_url(data), meta["brief"], key, get_settings().fal_text_model, transport))
                if verdict is not None and verdict["score"] <= 2:
                    conn.execute("UPDATE creatives SET status = 'failed', error = ? WHERE id = ?",
                                 ("Баннер получился слабым" + (": " + "; ".join(verdict["issues"]) if verdict["issues"] else "") + ". Нажмите «Другой баннер».", r["id"]))
                    continue
                meta["score"] = verdict["score"] if verdict else 0
            asset_id = _save_asset(conn, business_id, "image" if r["kind"] == "ai_banner" else r["kind"], data, f"creative-{r['id']}")
            conn.execute("UPDATE creatives SET status = 'draft', asset_id = ?, external_id = '', prompt = ? WHERE id = ?",
                         (asset_id, json.dumps(meta, ensure_ascii=False) if meta else r["prompt"], r["id"]))
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
        "prices_usd": {"image": fal.COST_CENTS["image"] / 100, "video": fal.COST_CENTS["video"] / 100,
                       "ai_banner": fal.COST_CENTS["ai_banner"] / 100},
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
        conn.execute("INSERT INTO creatives (business_id, kind, title, body, source, angle) VALUES (?, 'text', ?, ?, ?, ?)",
                     (business_id, v["title"], v["text"], source, v.get("angle", "")))
        added += 1
    conn.commit()
    has_ai = bool(s.anthropic_api_key or s.fal_key)
    return {"added": added, "ai": ai_count, "used_ai": has_ai,
            "message": ("Придумали варианты с помощью ИИ." if ai_count else
                        "Варианты собраны по шаблону: ИИ для текстов пока не подключён." if not has_ai else
                        "ИИ не ответил, поэтому варианты собраны по шаблону. Попробуйте ещё раз позже.") if added else
                       "Новых вариантов не нашлось: такие заголовки уже есть."}


def _ad_variants(conn, biz) -> tuple[list[dict], int, int]:
    """Объявления, которые будут созданы при запуске: те же тексты и то же число, что в плане запуска."""
    candidates = text_candidates(conn, biz)
    pool, count = start_split(biz)
    shown = count or 3  # бюджет слишком мал для запуска: всё равно показываем, как будет выглядеть
    variants, _ = choose_variants(biz, candidates, shown)
    known = {c["title"].lower(): c for c in candidates}
    for v in variants:
        c = known.get(v["title"].lower())
        v["creative_id"] = c["id"] if c else None
        v["status"] = c["status"] if c else "template"
    return variants, count, pool


@router.get("/ads")
def ads(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Готовые объявления: текст вместе с картинкой, как они уйдут в Директ."""
    biz = _own(conn, user, business_id)
    variants, count, pool = _ad_variants(conn, biz)
    photos, _logo = media_typed(conn, biz)
    items = []
    for i, v in enumerate(variants):
        style = style_for(i)
        ai = None
        photo_type = photo_for(photos, i)[0] if photos and style != "ai" else ""
        banner = f"/api/businesses/{business_id}/creatives/ads/{i}/banner.jpg" if photos else ""
        row = _ai_row(conn, business_id, v)
        state = "none" if row is None else "working" if row["status"] == "working" else "failed" if row["status"] == "failed" else "ready"
        ai = {"status": state, "error": row["error"] if row is not None and state == "failed" else ""}
        meta = json.loads(row["prompt"] or "{}") if row is not None and state != "none" else {}
        if state == "ready" and meta.get("concept"):  # сюжет баннера: по нему потом сравниваем, какая сцена и ЦА работают лучше
            photo_type = meta["concept"]
            ai.update({"scene": scene_label(photo_type), "audience": meta.get("audience", ""),
                       "logo": "placed" if meta.get("logo") else "skipped", "logo_reason": meta.get("logo_reason", "")})
        if state == "ready":
            banner = f"/api/businesses/{business_id}/creatives/ads/{i}/banner.jpg"   # готовый ИИ-баннер важнее шаблона
        elif style == "ai":
            banner = ""
        items.append({"index": i, "title": v["title"], "text": v["text"], "own": v["status"] == "approved",
                      "creative_id": v["creative_id"], "text_status": v["status"], "style": style, "ai": ai, "banner_url": banner,
                      "angle": v.get("angle", ""), "angle_label": angle_label(v.get("angle", "")),
                      "photo_type": photo_type, "photo_label": PHOTO_TAGS.get(photo_type) or scene_label(photo_type)})
    return {
        "ads": items,
        "count": count,
        "per_unit": round(pool / count / 1_000_000) if count else 0,
        "site": biz["site_url"] or "",
        "has_maps": bool(biz["maps_url"]),
        "has_images": bool(photos),
        "rejected_photos": conn.execute("SELECT COUNT(*) FROM business_assets WHERE business_id = ? AND kind = 'photo' AND ai_score BETWEEN 1 AND 2",
                                        (business_id,)).fetchone()[0],
        "cta": profile(biz)["cta"],
    }


def _screenshot(conn, business_id: int) -> bytes | None:
    """Скриншот приложения, который владелец загрузил (тип «Скриншот приложения»): его вставляем в экран телефона на баннерах."""
    row = conn.execute("SELECT stored_name FROM business_assets WHERE business_id = ? AND kind = 'photo' AND tag = 'screen' ORDER BY id DESC LIMIT 1",
                       (business_id,)).fetchone()
    try:
        return _asset_path(business_id, row["stored_name"]).read_bytes() if row else None
    except OSError:
        return None


def _logo_ratio(logo: bytes | None) -> float | None:
    """Пропорции логотипа (ширина к высоте), чтобы ИИ оставил слот подходящей формы."""
    if not logo:
        return None
    try:
        w, h = Image.open(BytesIO(logo)).size
    except (OSError, ValueError):
        return None
    return max(1.0, min(5.0, w / h)) if h else None


def _ai_row(conn, business_id: int, variant: dict):
    """Последний заказ ИИ-баннера под этот вариант объявления (по заголовку и тексту)."""
    return conn.execute(
        "SELECT * FROM creatives WHERE business_id = ? AND kind = 'ai_banner' AND title = ? AND body = ? ORDER BY id DESC LIMIT 1",
        (business_id, variant["title"], variant["text"])).fetchone()


def _logo_info(conn, biz, logo: bytes | None) -> dict | None:
    """Что арт-директору известно о логотипе: пропорции и оценка читаемости от ИИ-проверки."""
    if logo is None:
        return None
    row = conn.execute("SELECT ai_score, ai_note FROM business_assets WHERE business_id = ? AND kind = 'logo'", (biz["id"],)).fetchone()
    return {"ratio": _logo_ratio(logo) or 2.5, "score": row["ai_score"] if row else 0, "note": (row["ai_note"] if row else "")[:200]}


def _other_scenes(conn, business_id: int, variants: list[dict], skip: int) -> list[str]:
    scenes = []
    for i, other in enumerate(variants):
        row = _ai_row(conn, business_id, other) if i != skip else None
        if row is not None and row["status"] != "failed":
            scene = json.loads(row["prompt"] or "{}").get("scene")
            if scene:
                scenes.append(scene[:160])
    return scenes


def _order_banner(conn, biz, variants: list[dict], index: int, key: str, transport, others: list[str]) -> str:
    """Заказывает один ИИ-баннер: арт-директор придумывает сцену и решает про логотип, потом рисует художник. Возвращает сцену."""
    business_id = biz["id"]
    v = variants[index]
    s = get_settings()
    price = find_price(v["title"], v["text"], biz["description"])
    cta = profile(biz)["cta"]
    expected = [v["title"], biz["name"], cta] + ([price] if price else [])
    logo = logo_for(conn, biz)
    screen = _screenshot(conn, business_id) is not None
    art = asyncio.run(art_direction(biz, v, index, others, _logo_info(conn, biz, logo), screen, key, s.fal_text_model, transport))
    use_logo = bool(art["logo"]) and logo is not None
    prompt = fal.banner_prompt(biz, v["title"], price, cta, logo_corner=use_logo, index=index, screen=screen,
                               logo_ratio=_logo_ratio(logo), scene=art["scene"])
    job = asyncio.run(fal.start_image(prompt, key, s.fal_banner_model, transport))
    meta = {"expect": expected, "logo": use_logo, "concept": art["concept"], "audience": art["audience"], "scene": art["scene"],
            "logo_reason": art["logo_reason"], "brief": f"{biz['kind']} «{biz['name']}». Сюжет: {art['label']}. {art['scene']}"}
    conn.execute("INSERT INTO creatives (business_id, kind, title, body, prompt, status, source, external_id) VALUES (?, 'ai_banner', ?, ?, ?, 'working', 'fal', ?)",
                 (business_id, v["title"], v["text"], json.dumps(meta, ensure_ascii=False), json.dumps(job)))
    return art["scene"]


@router.post("/ads/{index}/ai-banner")
def make_ai_banner(business_id: int, index: int, user: sqlite3.Row = Depends(current_user),
                   conn: sqlite3.Connection = Depends(get_db), transport=Depends(creative_transport)):
    """Заказывает баннер целиком у нейросети (стиль «ИИ-дизайн»). Платно: цену показывает сайт перед запуском."""
    biz = _own(conn, user, business_id)
    key = _need_fal()
    variants, _count, _pool = _ad_variants(conn, biz)
    if not 0 <= index < len(variants):
        raise HTTPException(400, detail={"message": "Такого объявления нет."})
    old = _ai_row(conn, business_id, variants[index])
    if old is not None and old["status"] == "working":
        raise HTTPException(400, detail={"message": "Этот баннер уже рисуется. Подождите минуту."})
    _charge(conn, business_id, "ai_banner")
    try:
        _order_banner(conn, biz, variants, index, key, transport, _other_scenes(conn, business_id, variants, index))
    except fal.FalError as e:
        conn.rollback()
        raise HTTPException(502, detail={"message": e.message})
    conn.commit()
    return {"started": True}


@router.post("/ads/{index}/rewrite")
def rewrite_text(business_id: int, index: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
                 transport=Depends(creative_transport)):
    """Другой текст для этого места: ИИ пишет новый вариант (желательно на тот же угол подачи), он встаёт на место старого."""
    biz = _own(conn, user, business_id)
    s = get_settings()
    if not (s.anthropic_api_key or s.fal_key):
        raise HTTPException(503, detail={"message": "ИИ для текстов пока не подключён на сервере."})
    variants, _count, _pool = _ad_variants(conn, biz)
    if not 0 <= index < len(variants):
        raise HTTPException(400, detail={"message": "Такого объявления нет."})
    old = variants[index]
    used = {r["title"].lower() for r in conn.execute("SELECT title FROM creatives WHERE business_id = ? AND kind = 'text'", (business_id,))}
    used |= {v["title"].lower() for v in variants}
    _charge(conn, business_id, "text")
    hint = (f"Нужен новый вариант взамен этого, не повторяй его мысль и слова: «{old['title']}». "
            + (f"Угол подачи: {old['angle']}." if old.get("angle") else ""))
    fresh = asyncio.run(generate_texts(biz, 4, s.anthropic_api_key, s.anthropic_model, hint, transport, s.fal_key, s.fal_text_model))
    fresh = [v for v in fresh if v["title"].lower() not in used]
    same = [v for v in fresh if old.get("angle") and v["angle"] == old["angle"]]
    pick = (same or fresh or [None])[0]
    if pick is None:
        conn.execute("DELETE FROM generations WHERE id = (SELECT MAX(id) FROM generations WHERE business_id = ? AND kind = 'text')", (business_id,))
        conn.commit()
        raise HTTPException(502, detail={"message": "ИИ не предложил подходящий новый вариант. Попробуйте ещё раз."})
    if old.get("creative_id"):   # тот же номер в списке: объявление не прыгает на другое место
        conn.execute("UPDATE creatives SET title = ?, body = ?, angle = ?, status = 'approved', source = 'claude' WHERE id = ?",
                     (pick["title"], pick["text"], pick.get("angle", ""), old["creative_id"]))
    else:
        conn.execute("INSERT INTO creatives (business_id, kind, title, body, source, angle, status) VALUES (?, 'text', ?, ?, 'claude', ?, 'approved')",
                     (business_id, pick["title"], pick["text"], pick.get("angle", "")))
    conn.commit()
    return {"title": pick["title"], "text": pick["text"], "angle": pick.get("angle", "")}


@router.post("/ads/ai-banners")
def make_all_ai_banners(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
                        transport=Depends(creative_transport)):
    """Заказывает ИИ-баннеры для всех объявлений, у которых готового баннера ещё нет. Платно: сайт заранее показывает общую цену."""
    biz = _own(conn, user, business_id)
    key = _need_fal()
    variants, _count, _pool = _ad_variants(conn, biz)
    others = _other_scenes(conn, business_id, variants, -1)
    started = 0
    for index, v in enumerate(variants):
        old = _ai_row(conn, business_id, v)
        if old is not None and old["status"] in ("working", "draft", "approved"):
            continue
        _charge(conn, business_id, "ai_banner")
        try:
            others.append(_order_banner(conn, biz, variants, index, key, transport, others[-6:])[:160])
        except fal.FalError as e:
            conn.rollback()
            if started:
                conn.commit()
            raise HTTPException(502, detail={"message": e.message})
        started += 1
    conn.commit()
    return {"started": started}


_BANNERS: dict[tuple, bytes] = {}  # готовые баннеры: сборка занимает около секунды, а страницу открывают часто


@router.get("/ads/{index}/banner.jpg")
def ad_banner(business_id: int, index: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    biz = _own(conn, user, business_id)
    variants, _count, _pool = _ad_variants(conn, biz)
    photos, logo = media_typed(conn, biz)
    ai_row = _ai_row(conn, business_id, variants[index]) if 0 <= index < len(variants) else None
    asset = conn.execute("SELECT stored_name FROM business_assets WHERE id = ?", (ai_row["asset_id"],)).fetchone() if ai_row and ai_row["asset_id"] else None
    ready = asset is not None and ai_row["status"] in ("draft", "approved")
    if 0 <= index < len(variants) and (ready or style_for(index) == "ai"):
        if not ready:
            raise HTTPException(404, detail={"message": "ИИ-баннер ещё не готов."})
        row = ai_row
        picture = prepare_for_direct(_asset_path(business_id, asset["stored_name"]).read_bytes())
        if not json.loads(row["prompt"] or "{}").get("baked"):  # старые баннеры без вшитого логотипа
            picture = add_logo(picture, logo)
        return Response(picture, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=60"})
    if not photos or not 0 <= index < len(variants):
        raise HTTPException(404, detail={"message": "Для этого объявления нет картинки."})
    photo = photo_for(photos, index)[1]
    v = variants[index]
    key = (hashlib.sha1(photo).hexdigest(), v["title"], v["text"], biz["name"], biz["description"], biz["color_main"],
           banner_layout(index), hashlib.sha1(logo).hexdigest() if logo else "")
    banner = _BANNERS.get(key)
    if banner is None:
        banner = render_banner(biz, v, photo, logo, index)
        if len(_BANNERS) >= 48:
            _BANNERS.pop(next(iter(_BANNERS)))
        _BANNERS[key] = banner
    return Response(banner, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=60"})


def _data_url(path) -> str:
    """Фото для модели со зрением: уменьшенное, чтобы запрос был быстрым и дешёвым."""
    img = Image.open(path)
    img.load()
    img.thumbnail((1024, 1024))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        base = Image.new("RGB", img.size, (255, 255, 255))
        base.paste(img, mask=img.split()[-1])
        img = base
    out = BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


@router.post("/photos/check")
def check_photos(business_id: int, force: bool = False, user: sqlite3.Row = Depends(current_user),
                 conn: sqlite3.Connection = Depends(get_db), transport=Depends(creative_transport)):
    """ИИ смотрит на загруженные фото и логотип: тип кадра, пригодность для рекламы, проблемы (чужие вывески, водяные знаки)."""
    _own(conn, user, business_id)
    key = _need_fal()
    s = get_settings()
    rows = conn.execute(
        "SELECT * FROM business_assets WHERE business_id = ? AND kind IN ('photo', 'logo', 'generated') AND tag != 'screen' "
        "AND id NOT IN (SELECT asset_id FROM creatives WHERE kind = 'ai_banner' AND asset_id IS NOT NULL) "
        + ("" if force else "AND ai_score = 0 ") + "ORDER BY id LIMIT 10", (business_id,)).fetchall()
    checked = failed = 0
    for r in rows:
        _charge(conn, business_id, "check")
        try:
            result = asyncio.run(check_photo(_data_url(_asset_path(business_id, r["stored_name"])), key, s.fal_text_model, transport,
                                             logo=r["kind"] == "logo", synthetic=r["kind"] == "generated"))
        except (OSError, ValueError):
            result = None
        if result is None:
            conn.execute("DELETE FROM generations WHERE id = (SELECT MAX(id) FROM generations WHERE business_id = ? AND kind = 'check')", (business_id,))
            failed += 1
            continue
        conn.execute("UPDATE business_assets SET ai_type = ?, ai_score = ?, ai_note = ? WHERE id = ?",
                     (result["type"], result["score"], result["note"], r["id"]))
        checked += 1
    conn.commit()
    return {"checked": checked, "failed": failed}


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
    count: int = Field(default=5, ge=1, le=5)
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
    made = conn.execute("SELECT COUNT(*) FROM creatives WHERE business_id = ? AND kind = 'image' AND status != 'failed'",
                        (business_id,)).fetchone()[0]
    for i in range(data.count):
        _charge(conn, business_id, "image")
        prompt = fal.image_prompt(biz, data.idea, made + i)  # каждая следующая картинка в другом стиле
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
