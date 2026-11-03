"""Креативы: тексты, картинки и видео для объявлений."""
import asyncio
import base64
import hashlib
import json
import os
import re
import sqlite3
from io import BytesIO
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from PIL import Image
from pydantic import BaseModel, Field

from app.assets import AssetError, prepare_image, prepare_video
from app.config import get_settings
from app.creative import fal, refs
from app.creative.angles import effective_description, label as angle_label, profile
from app.assets import PHOTO_TAGS
from app.creative.compose import add_caption, add_logo, find_price, prepare_for_direct, style_for
from app.creative.art import art_direction, review_banner, scene_label
from app.creative.screen import insert_logo, insert_screenshot
from app.creative.texts_ai import (EXTRA_WORDS_OK, check_facts, check_photo, extra_words, facts_of, foreign_prices, generate_texts, invented_links,
                                   missing_phrases, read_image_text)
from app.db import get_db
from app.deps import creative_transport, current_user
from app.direct.launch import MAX_VARIANTS, ads_for_budget, banner_layout, choose_variants, logo_for, media_typed, photo_for, render_banner, start_split, text_candidates, usable_photos
from app.direct.texts import TEXT_MAX, TITLE_MAX, check, draft_variants
from app.routes.businesses import _asset_path, _own

import secrets

router = APIRouter(prefix="/api/businesses/{business_id}/creatives", tags=["creatives"])

STATUSES = ("draft", "approved", "rejected")
MAX_TEXTS = 30


def _spent_cents(conn, business_id: int, whole_time: bool = False) -> int:
    """Расход на генерацию: по умолчанию с 1-го числа текущего месяца (лимит обнуляется сам), whole_time=True: за всё время."""
    month = "" if whole_time else " AND created_at >= strftime('%Y-%m-01', 'now')"
    return conn.execute(f"SELECT COALESCE(SUM(cents),0) FROM generations WHERE business_id = ?{month}", (business_id,)).fetchone()[0]


def _charge(conn, business_id: int, kind: str) -> None:
    """Записывает расход на генерацию. Выше лимита генерировать не даём."""
    limit = round(get_settings().gen_limit_usd * 100)
    cents = fal.COST_CENTS[kind]
    if _spent_cents(conn, business_id) + cents > limit:
        raise HTTPException(400, detail={"message": f"Лимит расходов на генерацию в этом месяце исчерпан (${limit / 100:.0f}). Он обнулится 1-го числа. Администратор может увеличить его настройкой GEN_LIMIT_USD."})
    conn.execute("INSERT INTO generations (business_id, kind, cents) VALUES (?,?,?)", (business_id, kind, cents))


def _caps() -> dict:
    s = get_settings()
    return {"text_ai": bool(s.anthropic_api_key or s.fal_key), "image_ai": bool(s.fal_key), "video_ai": bool(s.fal_key)}


def _row(r: sqlite3.Row, business_id: int) -> dict:
    meta = {}
    if r["kind"] == "ai_banner":   # для истории баннеров: какая сцена, для кого, был ли логотип
        try:
            made = json.loads(r["prompt"] or "{}")
        except ValueError:
            made = {}
        meta = {"scene": scene_label(made.get("concept", "")), "audience": made.get("audience", ""), "logo": bool(made.get("logo")),
                "score": made.get("score", 0)}
    return {"meta": meta,
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


def _bytes_url(data: bytes, side: int = 1024) -> str:
    """Картинка в виде data-адреса для модели (уменьшенная: быстрее и дешевле)."""
    img = Image.open(BytesIO(data)).convert("RGB")
    img.thumbnail((side, side))
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
            if read is None:   # распознавание порой не отвечает с первого раза (на пробе у каждого четвёртого баннера): без повтора баннер прошёл бы непроверенным
                read = asyncio.run(read_image_text(value, key, s.fal_text_model, transport))
            wrong = missing_phrases(expected, read) if read is not None else []
            if wrong:
                conn.execute("UPDATE creatives SET status = 'failed', error = ? WHERE id = ?",
                             (f"На картинке ошибки в тексте: {', '.join(wrong)}. Нажмите «Нарисовать» ещё раз.", r["id"]))
                continue
            links = invented_links(read or [])
            if links:
                conn.execute("UPDATE creatives SET status = 'failed', error = ? WHERE id = ?",
                             (f"Нейросеть дописала на картинке адрес сайта: {', '.join(links)}. Нажмите «Другой баннер».", r["id"]))
                continue
            owner = conn.execute("SELECT * FROM businesses WHERE id = ?", (business_id,)).fetchone()
            extra = extra_words(expected, read or [])
            if len(extra) > EXTRA_WORDS_OK:
                conn.execute("UPDATE creatives SET status = 'failed', error = ? WHERE id = ?",
                             (f"Нейросеть дописала на картинке лишний текст: {', '.join(extra[:6])}. Нажмите «Другой баннер».", r["id"]))
                continue
            foreign = foreign_prices(expected, read or [])
            if foreign:   # сумма, которой нет в объявлении: либо дописала нейросеть, либо взяли не ту цену
                conn.execute("UPDATE creatives SET status = 'failed', error = ? WHERE id = ?",
                             (f"На картинке указана цена, которой нет в объявлении: {', '.join(foreign)}. Нажмите «Другой баннер».", r["id"]))
                continue
            if read:   # всё написанное на баннере сверяем с тем, что владелец рассказал о бизнесе: цены, сроки, условия не должны ему противоречить
                wrong = asyncio.run(check_facts(facts_of(owner), r["title"], r["body"], read, key, s.fal_text_model, transport))
                if wrong:
                    conn.execute("UPDATE creatives SET status = 'failed', error = ? WHERE id = ?",
                                 (f"На баннере сведения, которых нет в описании бизнеса или которые ему противоречат: {'; '.join(wrong)}. Нажмите «Другой баннер».", r["id"]))
                    continue
        try:
            data = asyncio.run(fal.download(value, transport))
            if r["kind"] == "ai_banner":
                shot = _screenshot(conn, business_id) if json.loads(r["prompt"] or "{}").get("screen") else None
                if shot:  # настоящий скриншот ложится в пурпурный экран телефона (только если баннер заказан с экраном)
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
            if r["kind"] == "ai_banner" and meta.get("caption"):   # «название · вид деятельности» дорисовывает код: так подпись всегда читается, без искажённых букв
                data = add_caption(data, meta["caption"])
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
    shown: dict[tuple, tuple] = {}   # какой готовый баннер сейчас на объявлении: у кого свежее «вернули» или создан
    for r in rows:
        if r["kind"] == "ai_banner" and r["status"] in ("draft", "approved") and r["asset_id"]:
            stamp = (r["picked_at"] or r["created_at"], r["id"])
            if (r["title"], r["body"]) not in shown or stamp > shown[(r["title"], r["body"])][0]:
                shown[(r["title"], r["body"])] = (stamp, r["id"])
    items = [_row(r, business_id) for r in rows]
    for item in items:
        item["current"] = item["kind"] == "ai_banner" and shown.get((item["title"], item["body"]), (None, None))[1] == item["id"]
    return {
        "creatives": items,
        "caps": _caps(),
        "spend": {"spent_usd": round(_spent_cents(conn, business_id) / 100, 2), "limit_usd": limit,
                  "total_usd": round(_spent_cents(conn, business_id, whole_time=True) / 100, 2)},
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
    shown = MAX_VARIANTS if count else 3   # показываем все варианты: первые count войдут в кампанию, остальные запасные. Денег не хватает на запуск: всё равно показываем, как будет выглядеть
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
            ai.update({"scene": meta["scene"] if meta.get("mode") == "photo" else scene_label(photo_type), "audience": meta.get("audience", ""),
                       "logo": "placed" if meta.get("logo") else "skipped", "logo_reason": meta.get("logo_reason", "")})
        brief = _brief_for(conn, business_id, v)
        if state in ("ready", "working") and meta:   # какой референс по-настоящему ушёл модели картинкой в последнем заказе
            sent = meta.get("ref") if meta.get("mode") == "photo" or meta.get("ref_img") else None
            ai["ref_sent"] = sent
            ai["ref_note"] = refs.note(sent) if sent else ""
        if brief:   # объявление сделано по идее стратега: показываем, что именно проверяем
            ai.update({"hypothesis": brief["hypothesis"], "audience": brief["audience"], "inspiration": refs.note(brief["ref_id"])})
        no_photo = bool(brief and brief["no_photo"])
        auto_ref = None if not _wants_auto_ref(brief, i) or _extras() else _auto_ref(conn, biz, i, v)   # подача, которую возьмём при первом заказе
        if photos and not _extras() and no_photo:   # без фото промпт собирается при заказе: показываем последний, если он был
            if meta.get("mode") == "scratch" and meta.get("prompt_text"):
                ai["prompt"] = meta["prompt_text"]
        elif photos and not _extras():   # промпт, по которому рисуется баннер из фото: виден и правится на карточке
            ai["prompt"] = (brief["prompt"] if brief and brief["prompt"] else None) or meta.get("prompt_text") or (
                _brief_prompt(biz, v["title"], v["text"], i % len(fal.PHOTO_LOOKS), auto_ref) if auto_ref else fal.photo_banner_prompt(
                    biz, v["title"], find_price(v["title"], v["text"], fallback=effective_description(biz)), profile(biz)["cta"], brief["look"] if brief else i))
        if state == "ready":
            banner = f"/api/businesses/{business_id}/creatives/ads/{i}/banner.jpg"   # готовый ИИ-баннер важнее шаблона
        elif style == "ai":
            banner = ""
        items.append({"index": i, "title": v["title"], "text": v["text"], "own": v["status"] == "approved",
                      "creative_id": v["creative_id"], "text_status": v["status"], "style": style, "ai": ai, "banner_url": banner,
                      "angle": v.get("angle", ""), "angle_label": angle_label(v.get("angle", "")),
                      "photo_type": photo_type, "photo_label": PHOTO_TAGS.get(photo_type) or scene_label(photo_type),
                      "photo_id": None if no_photo else (brief["asset_id"] if brief and brief["asset_id"] else getattr(photo_for(photos, i)[1], "asset_id", None) if photos else None),
                      "no_photo": no_photo, "reserve": bool(count) and i >= count,   # запасное: в кампанию войдёт, когда понадобится заменить слабое
                      "look_no": brief["look"] if brief else i % len(fal.PHOTO_LOOKS), "ref_id": brief["ref_id"] if brief and not auto_ref else auto_ref})
    return {
        "ads": items,
        "count": count,
        "advised": ads_for_budget(pool) if count else 0,   # сколько объявлений подходит бюджету
        "weekly": round(pool / 1_000_000) if count else 0,
        "site": biz["site_url"] or "",
        "has_maps": bool(biz["maps_url"]),
        "has_images": bool(photos),
        "rejected_photos": conn.execute("SELECT COUNT(*) FROM business_assets WHERE business_id = ? AND kind = 'photo' AND ai_score BETWEEN 1 AND 2",
                                        (business_id,)).fetchone()[0],
        "cta": profile(biz)["cta"],
        # для редактора баннера: из каких фото выбирать и какие подачи доступны
        "photos": [{"id": r["id"], "url": f"/api/businesses/{business_id}/assets/{r['id']}/file", "label": PHOTO_TAGS.get(r["ai_type"] or r["tag"], "Фото")}
                   for r in usable_photos(conn, biz)],
        "styles": {"looks": [{"no": n, "label": name} for n, (name, _d) in enumerate(fal.PHOTO_LOOKS)],
                   "refs": [{"id": n, "label": refs.note(n)} for n in _niche_refs(biz)]},   # те же приёмы, из которых сервис сам выбирает подачу объявлениям
        "ref_extra": fal.COST_CENTS["ref_extra"] / 100,
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
        "SELECT * FROM creatives WHERE business_id = ? AND kind = 'ai_banner' AND title = ? AND body = ? "
        "ORDER BY COALESCE(picked_at, created_at) DESC, id DESC LIMIT 1",
        (business_id, variant["title"], variant["text"])).fetchone()


def _ready_row(conn, business_id: int, variant: dict):
    """Последний готовый ИИ-баннер этого объявления (его видно, пока рисуется новая версия)."""
    return conn.execute(
        "SELECT * FROM creatives WHERE business_id = ? AND kind = 'ai_banner' AND title = ? AND body = ? "
        "AND status IN ('draft', 'approved') AND asset_id IS NOT NULL ORDER BY COALESCE(picked_at, created_at) DESC, id DESC LIMIT 1",
        (business_id, variant["title"], variant["text"])).fetchone()


def _extras() -> bool:
    """Расширения баннера (скриншот на экране телефона, логотип в слоте). Включаются переменной BANNER_EXTRAS=1."""
    return os.environ.get("BANNER_EXTRAS", "").strip() == "1"


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


def _brief_for(conn, business_id: int, variant: dict):
    """Идея стратега, по которой сделано это объявление (если владелец выбрал её в рекламу)."""
    return conn.execute("SELECT * FROM briefs WHERE business_id = ? AND title = ? AND body = ? AND status = 'used' ORDER BY id DESC LIMIT 1",
                        (business_id, variant["title"], variant["text"])).fetchone()


def _order_banner(conn, biz, variants: list[dict], index: int, key: str, transport, others: list[str], custom_prompt: str = "") -> str:
    """Заказывает один ИИ-баннер. Есть фото владельца: модель делает баннер из этого фото (оформление у каждого объявления своё).
    Фото нет: арт-директор придумывает сцену, художник рисует с нуля. custom_prompt: промпт, который владелец поправил сам. Возвращает описание."""
    business_id = biz["id"]
    v = variants[index]
    s = get_settings()
    price = find_price(v["title"], v["text"], fallback=biz["description"])
    cta = profile(biz)["cta"]
    expected = [v["title"], cta] + ([price] if price else [])   # на баннере только заголовок, кнопка и цена: подписи с названием нет
    extras = _extras()   # экран со скриншотом и логотип в слоте: пока выключены, сначала добиваемся хорошего базового баннера
    photos, _logo = media_typed(conn, biz)
    brief = _brief_for(conn, business_id, v)   # идея стратега или выбор владельца в редакторе: фото, оформление, референс, промпт
    no_photo = bool(brief and brief["no_photo"])   # владелец выбрал «без фото»: ИИ рисует всю сцену сам
    if photos and not extras and not no_photo:   # главный путь: настоящее фото владельца отдаётся модели вместе с промптом про оформление
        varied = ""
        if _wants_auto_ref(brief, index) and not custom_prompt:   # подача не выбрана: берём приём из ниши, у соседних объявлений разные
            brief = _auto_style(conn, biz, v, index, getattr(photo_for(photos, index)[1], "asset_id", None), brief) or brief
        if not custom_prompt:   # повторный заказ без правок подачи: берём другую подачу, чтобы вариант отличался от прежних
            pick = _vary_style(conn, biz, v, brief, index)
            if pick:
                ref_pick, look_pick = pick
                asset = brief["asset_id"] if brief and brief["asset_id"] else getattr(photo_for(photos, index)[1], "asset_id", None)
                look_keep = look_pick if look_pick is not None else (brief["look"] if brief else index % len(fal.PHOTO_LOOKS))
                _apply_style(conn, biz, v, brief, asset, look_keep, ref_pick)
                brief = _brief_for(conn, business_id, v)
                varied = refs.note(ref_pick) if ref_pick else f"оформление «{fal.PHOTO_LOOKS[look_keep][0]}»"
        kind, data = photo_for(photos, index)
        if brief and brief["asset_id"]:
            kind, data = next(((k, d) for k, d in photos if getattr(d, "asset_id", None) == brief["asset_id"]), (kind, data))
        look_no = brief["look"] if brief else index
        look = fal.PHOTO_LOOKS[look_no % len(fal.PHOTO_LOOKS)][0]
        prompt = custom_prompt or (brief["prompt"] if brief and brief["prompt"] else fal.photo_banner_prompt(biz, v["title"], price, cta, look_no))
        ref_bytes = refs.load(brief["ref_id"]) if brief and brief["ref_id"] else None   # референс-вдохновение идёт второй картинкой
        images = ([_bytes_url(ref_bytes, 900)] if ref_bytes else []) + [_bytes_url(data, 1280)]   # референс первым: он основа дизайна, фото владельца встаёт на место его кадра
        if ref_bytes:
            _charge(conn, business_id, "ref_extra")
        job = asyncio.run(fal.start_edit(prompt, images, key, s.fal_banner_model, transport))
        what = f"фото «{PHOTO_TAGS.get(kind, kind)}», " + (f"по мотивам референса {brief['ref_id']}" if ref_bytes else f"оформление: {look}")
        meta = {"expect": expected, "caption": fal.caption_for(biz), "logo": False, "screen": False, "mode": "photo", "concept": kind, "look": look,
                "look_no": look_no % len(fal.PHOTO_LOOKS), "photo_id": getattr(data, "asset_id", None), "varied": varied,
                "ref": brief["ref_id"] if ref_bytes else None,
                "audience": brief["audience"] if brief else "", "hypothesis": brief["hypothesis"] if brief else "", "scene": what,
                "logo_reason": "", "prompt_text": prompt,
                "brief": ("ВАЖНО: основа баннера настоящее фото владельца, его нужно принимать как есть. "
                          + (f"Баннер сделан по мотивам приёма «{refs.note(brief['ref_id'])}»: цвета и подача определяются этим приёмом, а не словами «светлое» "
                             "или «тёмное»; огромный заголовок или слово, которое частично перекрывается объектом, это задумка, а не недостаток. "
                             if ref_bytes else f"Оформление: {look}. ") + f"{biz['kind']}.")}
        conn.execute("INSERT INTO creatives (business_id, kind, title, body, prompt, status, source, external_id, created_at) "   # дата с долями секунды: сравнивается с датой возврата версии
                     "VALUES (?, 'ai_banner', ?, ?, ?, 'working', 'fal', ?, strftime('%Y-%m-%d %H:%M:%f', 'now'))",
                     (business_id, v["title"], v["text"], json.dumps(meta, ensure_ascii=False), json.dumps(job)))
        return what
    # баннер с нуля: фото нет или владелец выбрал «без фото». Сцену придумывает арт-директор, подача по референсу идёт словами
    varied = ""
    if _wants_auto_ref(brief, index) and not extras and not custom_prompt:   # подача не выбрана: берём приём из ниши
        brief = _auto_style(conn, biz, v, index, None, brief) or brief
    if not extras and not custom_prompt:   # повторный заказ без правок подачи берёт следующий референс
        pick = _vary_style(conn, biz, v, brief, index, scratch=True)
        if pick:
            ref_pick = pick[0]
            _apply_style(conn, biz, v, brief, None, brief["look"] if brief else index % len(fal.PHOTO_LOOKS), ref_pick, no_photo=no_photo)
            brief = _brief_for(conn, business_id, v)
            varied = refs.note(ref_pick) if ref_pick else "без референса"
    ref_id = brief["ref_id"] if brief and not extras else None
    done_before = len(_used_styles(conn, business_id, v, scratch=True))   # от этого зависит, какое из стандартных оформлений возьмём
    earlier = [json.loads(r["prompt"] or "{}") for r in conn.execute(
        "SELECT prompt FROM creatives WHERE business_id = ? AND kind = 'ai_banner' AND title = ? AND body = ? AND status IN ('draft', 'approved') "
        "ORDER BY id DESC LIMIT 6", (business_id, v["title"], v["text"]))]
    own_scenes = [m.get("scene", "")[:160] for m in earlier if m.get("mode") != "photo"][:3]   # прежние сцены этого же объявления: не повторять
    logo = logo_for(conn, biz) if extras else None
    screen = extras and _screenshot(conn, business_id) is not None
    art = asyncio.run(art_direction(biz, v, index, [x for x in others + own_scenes if x][-8:], _logo_info(conn, biz, logo), screen, key, s.fal_text_model, transport,
                                    refs.note(ref_id) if ref_id else ""))
    use_logo = bool(art["logo"]) and logo is not None
    # референс уходит модели картинкой: по одной строке слов сходства почти нет, а картинка задаёт композицию, цвет и шрифт
    ref_bytes = refs.load(ref_id) if ref_id else None
    if ref_bytes:
        _charge(conn, business_id, "ref_extra")
        direction = (f"the attached image is ONLY a style inspiration (the idea of its composition, color mood, typography weight and feel: {refs.note(ref_id)}). "
                     "Recreate its design closely: the same composition, the same size, weight and position of the headline (including a huge headline overlapping the subject), "
                     "the same color palette and the same overlay, transparency and frame effects; only the scene is new, the scene above. Do not copy or reuse its text, logo, products, people or characters, "
                     "and do not recreate its small print, captions, lists, badges, footer or contact lines: the only text on the banner is the text given below")
    else:
        direction = (f"{refs.note(ref_id)}. Take this advertising idea only as inspiration for the layout, color mood and type weight; "
                     "make an original design with your own scene and do not reuse any text, logo or character from it") if ref_id else ""
    prompt = custom_prompt or fal.banner_prompt(biz, v["title"], price, cta, logo_corner=use_logo, index=index + done_before, screen=screen,
                                                logo_ratio=_logo_ratio(logo), scene=art["scene"], direction=direction)
    job = (asyncio.run(fal.start_edit(prompt, [_bytes_url(ref_bytes, 900)], key, s.fal_banner_model, transport)) if ref_bytes
           else asyncio.run(fal.start_image(prompt, key, s.fal_banner_model, transport)))
    meta = {"expect": expected, "caption": fal.caption_for(biz), "logo": use_logo, "screen": screen, "concept": art["concept"], "audience": art["audience"], "scene": art["scene"],
            "mode": "scratch", "ref": ref_id, "ref_img": bool(ref_bytes), "varied": varied,
            "logo_reason": art["logo_reason"], "prompt_text": prompt,
            "brief": ("ВАЖНО: на экране телефона вставлен НАСТОЯЩИЙ скриншот приложения владельца. Это не выдумка нейросети и не недостаток: "
                      "интерфейс на экране, его вид и надписи на нём не оценивай и не снижай за них оценку. "
                      if screen else
                      "ВАЖНО: экран телефона намеренно не показан, настоящего скриншота нет, это не недостаток. ")
                     + ("Логотип вставлен позже из файла владельца, это нормально. " if use_logo else "")
                     + (f"Баннер сделан по мотивам приёма «{refs.note(ref_id)}»: подача, цвета и шрифт определяются этим приёмом, крупный заголовок это задумка, а не недостаток. "
                        if ref_id else "")
                     + f"{biz['kind']} «{biz['name']}». Сюжет: {art['label']}. {art['scene']}"}
    conn.execute("INSERT INTO creatives (business_id, kind, title, body, prompt, status, source, external_id, created_at) "   # дата с долями секунды: сравнивается с датой возврата версии
                     "VALUES (?, 'ai_banner', ?, ?, ?, 'working', 'fal', ?, strftime('%Y-%m-%d %H:%M:%f', 'now'))",
                 (business_id, v["title"], v["text"], json.dumps(meta, ensure_ascii=False), json.dumps(job)))
    return art["scene"]


class BannerIn(BaseModel):
    prompt: str = Field(default="", max_length=4000)   # свой промпт: владелец поправил предложенный и просит нарисовать по нему


@router.post("/ads/{index}/ai-banner")
def make_ai_banner(business_id: int, index: int, data: BannerIn | None = None, user: sqlite3.Row = Depends(current_user),
                   conn: sqlite3.Connection = Depends(get_db), transport=Depends(creative_transport)):
    """Заказывает баннер целиком у нейросети (стиль «ИИ-дизайн»). Платно: цену показывает сайт перед запуском."""
    biz = _own(conn, user, business_id)
    key = _need_fal()
    variants, _count, _pool = _ad_variants(conn, biz)
    if not 0 <= index < len(variants):
        raise HTTPException(400, detail={"message": "Такого объявления нет."})
    custom = (data.prompt.strip() if data else "")
    if custom and len(custom) < 30:
        raise HTTPException(400, detail={"message": "Промпт слишком короткий: опишите, какой должен быть баннер."})
    old = _ai_row(conn, business_id, variants[index])
    if old is not None and old["status"] == "working":
        raise HTTPException(400, detail={"message": "Этот баннер уже рисуется. Подождите минуту."})
    _charge(conn, business_id, "ai_banner")
    try:
        _order_banner(conn, biz, variants, index, key, transport, _other_scenes(conn, business_id, variants, index), custom)
    except fal.FalError as e:
        conn.rollback()
        raise HTTPException(502, detail={"message": e.message})
    conn.commit()
    row = _ai_row(conn, business_id, variants[index])
    varied = json.loads(row["prompt"] or "{}").get("varied", "") if row is not None else ""
    return {"started": True, **({"varied": varied} if varied else {})}   # varied: какую подачу взяли вместо прежней


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
def make_all_ai_banners(business_id: int, redo: bool = False, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
                        transport=Depends(creative_transport)):
    """Заказывает ИИ-баннеры для всех объявлений списка (и кампании, и запасных), у которых готового баннера ещё нет. Платно: сайт заранее показывает общую цену.
    redo: нарисовать заново и те, у которых баннер уже есть (каждый берёт следующую подачу, прежние версии остаются в списке версий)."""
    biz = _own(conn, user, business_id)
    key = _need_fal()
    variants, count, _pool = _ad_variants(conn, biz)
    others = _other_scenes(conn, business_id, variants, -1)
    started = 0
    for index, v in enumerate(variants):
        old = _ai_row(conn, business_id, v)
        if old is not None and (old["status"] == "working" or (old["status"] in ("draft", "approved") and not redo)):
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
    if ai_row is not None and ai_row["status"] in ("working", "failed"):   # пока рисуется новая версия (или не вышла), показываем прошлую готовую
        ai_row = _ready_row(conn, business_id, variants[index]) or ai_row
    asset = conn.execute("SELECT stored_name FROM business_assets WHERE id = ?", (ai_row["asset_id"],)).fetchone() if ai_row and ai_row["asset_id"] else None
    ready = asset is not None and ai_row["status"] in ("draft", "approved")
    if 0 <= index < len(variants) and (ready or style_for(index) == "ai"):
        if not ready:
            raise HTTPException(404, detail={"message": "ИИ-баннер ещё не готов."})
        row = ai_row
        picture = prepare_for_direct(_asset_path(business_id, asset["stored_name"]).read_bytes())
        made = json.loads(row["prompt"] or "{}")
        if not made.get("baked") and "concept" not in made:  # только старые баннеры: у новых решение про логотип принято при заказе
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
    biz = _own(conn, user, business_id)
    key = _need_fal()
    s = get_settings()
    # забракованное (1–2) проверяем заново, если название бизнеса с тех пор менялось: причиной могла быть «чужая» вывеска, а она своя
    rows = conn.execute(
        "SELECT * FROM business_assets WHERE business_id = ? AND kind IN ('photo', 'logo', 'generated') AND tag != 'screen' "
        "AND id NOT IN (SELECT asset_id FROM creatives WHERE kind = 'ai_banner' AND asset_id IS NOT NULL) "
        + ("" if force else "AND (ai_score = 0 OR (kind != 'logo' AND ai_fx IS NULL) OR (ai_score BETWEEN 1 AND 2 AND ai_brand != ?)) ")
        + "ORDER BY id LIMIT 10", (business_id,) if force else (business_id, biz["name"])).fetchall()
    checked = failed = 0
    for r in rows:
        _charge(conn, business_id, "check")
        try:
            result = asyncio.run(check_photo(_data_url(_asset_path(business_id, r["stored_name"])), key, s.fal_text_model, transport,
                                             logo=r["kind"] == "logo", synthetic=r["kind"] == "generated", brand=biz["name"]))
        except (OSError, ValueError):
            result = None
        if result is None:
            conn.execute("DELETE FROM generations WHERE id = (SELECT MAX(id) FROM generations WHERE business_id = ? AND kind = 'check')", (business_id,))
            failed += 1
            conn.commit()   # по файлу: запись в базе не должна висеть, пока ИИ смотрит следующие
            continue
        conn.execute("UPDATE business_assets SET ai_type = ?, ai_score = ?, ai_note = ?, ai_fx = ?, ai_fy = ?, ai_brand = ? WHERE id = ?",
                     (result["type"], result["score"], result["note"], result.get("fx"), result.get("fy"), biz["name"], r["id"]))
        checked += 1
        conn.commit()
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


def _brief_prompt(biz, title: str, text: str, look: int, ref_id: int | None) -> str:
    """Промпт баннера из фото для выбранной подачи: по мотивам референса или в одном из обычных оформлений."""
    price = find_price(title, text, fallback=effective_description(biz))
    cta = profile(biz)["cta"]
    if ref_id:
        return fal.reference_banner_prompt(biz, title, price, cta, f"take inspiration from this idea: {refs.note(ref_id)}")
    return fal.photo_banner_prompt(biz, title, price, cta, look)


def _apply_style(conn, biz, v: dict, brief, asset_id: int | None, look: int, ref_id: int | None, no_photo: bool = False, chosen: bool = False, keep_prompt: str = "") -> None:
    """Сохраняет фото и подачу объявления как идею (brief): по ней потом рисуется баннер.
    no_photo: баннер без готового фото, ИИ рисует всю сцену с нуля (промпт тогда строится при заказе).
    chosen: подачу выбрал сам владелец, автоподбор референса её потом не трогает."""
    prompt = _brief_prompt(biz, v["title"], v["text"], look, ref_id) if asset_id and not no_photo else ""
    if keep_prompt and prompt:   # промпт идеи стратега (настроение, подача) не теряем: к нему добавляется только указание про референс
        prompt = f"{keep_prompt} {fal.REF_STYLE}Direction: take inspiration from this idea: {refs.note(ref_id)}."
    if brief:
        conn.execute("UPDATE briefs SET asset_id = ?, look = ?, ref_id = ?, prompt = ?, no_photo = ?, chosen = MAX(chosen, ?) WHERE id = ?",
                     (None if no_photo else asset_id, look, ref_id, prompt, int(no_photo), int(chosen), brief["id"]))
    else:
        conn.execute("INSERT INTO briefs (business_id, angle, title, body, asset_id, look, prompt, ref_id, no_photo, chosen, status) VALUES (?,?,?,?,?,?,?,?,?,?, 'used')",
                     (biz["id"], v.get("angle", ""), v["title"], v["text"], None if no_photo else asset_id, look, prompt, ref_id, int(no_photo), int(chosen)))


_PROMO_NOTE = re.compile(r"цен|акци|скидк|%|срочн", re.IGNORECASE)
REF_ADS = 3   # по референсам рисуются только три первых объявления, у остальных обычное оформление
MIN_NICHE_REFS = REF_ADS   # у каждого из них должен быть свой приём


def _niche_refs(biz, limit: int = 8) -> list[int]:
    """Приёмы из ниши бизнеса. Пока в описании не разрешены скидки и акции, приёмы про цены и акции не берём."""
    found = refs.suggest(f"{biz['name']} {biz['kind']} {effective_description(biz)}", limit=12, focus=f"{biz['name']} {biz['kind']}", wide=True)
    if not biz["allow_promo"]:
        found = [r for r in found if not _PROMO_NOTE.search(refs.note(r))]
        if len(found) < MIN_NICHE_REFS:   # у зоотоваров без акций остаётся пять приёмов: добираем любыми приёмами без цен, чтобы хватило на все объявления
            found += [r for r in refs.REFS if r not in found and not _PROMO_NOTE.search(refs.note(r))][:MIN_NICHE_REFS - len(found)]
    return found[:limit]


def _taken_refs(conn, business_id: int, variant: dict) -> set[int]:
    """Приёмы, которые уже стоят у соседних объявлений."""
    return {r["ref_id"] for r in conn.execute("SELECT ref_id FROM briefs WHERE business_id = ? AND status = 'used' AND ref_id IS NOT NULL "
                                              "AND NOT (title = ? AND body = ?)", (business_id, variant["title"], variant["text"]))}


def _auto_ref(conn, biz, index: int, variant: dict) -> int | None:
    """Приём для объявления, у которого подача ещё не выбрана: у соседних объявлений разные, когда приёмы кончаются, берём обычное оформление."""
    found = _niche_refs(biz)
    if index >= REF_ADS or index >= len(found):   # по референсу рисуются только первые объявления, остальные в обычном оформлении
        return None
    taken = _taken_refs(conn, biz["id"], variant)
    return found[index] if found[index] not in taken else next((r for r in found if r not in taken), found[index])


def _wants_auto_ref(brief, index: int) -> bool:
    """Нужно ли подобрать приём из ниши: подачу не выбирали, либо у идеи стратега референса нет (владелец его не отменял)."""
    return brief is None or (not brief["ref_id"] and not brief["chosen"])


def _auto_style(conn, biz, v: dict, index: int, asset_id: int | None = None, brief=None):
    """Первый заказ баннера без выбранной подачи: сохраняем приём из ниши как подачу объявления (она видна в редакторе).
    Идея стратега без референса получает приём, остальное в ней (гипотеза, фото) остаётся."""
    ref = _auto_ref(conn, biz, index, v)
    if ref is None:
        return None
    if brief:
        _apply_style(conn, biz, v, brief, brief["asset_id"] or asset_id, brief["look"], ref, no_photo=bool(brief["no_photo"]), keep_prompt=brief["prompt"] or "")
    else:
        _apply_style(conn, biz, v, None, asset_id, index % len(fal.PHOTO_LOOKS), ref)
    return _brief_for(conn, biz["id"], v)


def _style_options(biz, scratch: bool = False, index: int = 0) -> list[tuple[int | None, int | None]]:
    """Подачи по порядку, (референс, оформление): сначала приёмы из ниши бизнеса, потом обычные оформления.
    Без фото обычных оформлений нет (они про кадр с фото): есть только референсы и «без референса».
    Объявления после первых трёх по референсам не рисуются: у них только обычные оформления."""
    found = [(r, None) for r in _niche_refs(biz)] if index < REF_ADS else []
    return found + ([(None, None)] if scratch else [(None, n) for n in range(len(fal.PHOTO_LOOKS))])


def _used_styles(conn, business_id: int, variant: dict, scratch: bool = False) -> list[tuple[int | None, int | None]]:
    """Какие подачи уже рисовались для этого объявления, от старой к новой (без неудачных).
    Баннеры из фото и баннеры с нуля считаются отдельно."""
    used = []
    for row in conn.execute("SELECT prompt FROM creatives WHERE business_id = ? AND kind = 'ai_banner' AND title = ? AND body = ? "
                            "AND status IN ('draft', 'approved', 'working') ORDER BY id", (business_id, variant["title"], variant["text"])):
        meta = json.loads(row["prompt"] or "{}")
        if (meta.get("mode") == "photo") == scratch:
            continue
        if scratch:
            if meta.get("ref") and not meta.get("ref_img"):   # приём передавали одними словами: по-настоящему он ещё не рисовался
                continue
            used.append((meta.get("ref"), None))
            continue
        ref, look = meta.get("ref"), meta.get("look_no")
        if look is None:   # старые заказы хранили только название оформления
            look = next((n for n, (name, _d) in enumerate(fal.PHOTO_LOOKS) if name == meta.get("look")), None)
        used.append((ref, None if ref else look))
    return used


def _vary_style(conn, biz, variant: dict, brief, index: int, scratch: bool = False) -> tuple[int | None, int | None] | None:
    """«Нарисовать заново» без правок подачи: берём следующую подачу, которой для этого объявления ещё не рисовали.
    Если владелец сам поменял подачу после прошлой отрисовки, возвращает None: рисуем так, как он выбрал."""
    used = _used_styles(conn, biz["id"], variant, scratch)
    if not used:
        return None
    ref = brief["ref_id"] if brief else None
    look = brief["look"] if brief else index % len(fal.PHOTO_LOOKS)
    current = (ref, None) if scratch else (ref, None if ref else look)
    if current != used[-1]:
        return None
    options = _style_options(biz, scratch, index)
    taken = _taken_refs(conn, biz["id"], variant)   # по возможности не повторяем приёмы соседних объявлений, чтобы они отличались друг от друга
    last_drawn = lambda o: max((i for i, u in enumerate(used) if u == o), default=-1)   # -1: этому объявлению ещё не рисовали
    pick = lambda pool: min(pool, key=lambda o: (last_drawn(o), options.index(o)))   # сначала то, что не рисовали, потом то, что рисовали давнее всех
    others = [o for o in options if o != current]
    free = [o for o in others if o[0] is None or o[0] not in taken]
    if scratch:
        # без фото «без референса» это не другая подача, а её отсутствие: объявление, которое уже рисовалось по приёму, его не теряет.
        # Свободных приёмов нет: лучше повторить приём соседнего объявления, чем остаться без референса
        pool = [o for o in free if o[0]] or ([o for o in others if o[0]] if current[0] else [])
        return pick(pool) if pool else None
    if free:
        return pick(free)
    return pick(others) if others else None


class SettingsIn(BaseModel):
    asset_id: int | None = None   # какое фото взять
    look: int | None = Field(default=None, ge=0, le=4)   # обычное оформление (сбрасывает референс)
    ref_id: int | None = None     # подача по мотивам референса из каталога
    no_photo: bool | None = None  # True: без готового фото, ИИ рисует всю сцену с нуля; выбор фото или оформления возвращает режим с фото


@router.put("/ads/{index}/settings")
def ad_settings(business_id: int, index: int, data: SettingsIn, user: sqlite3.Row = Depends(current_user),
                conn: sqlite3.Connection = Depends(get_db)):
    """Фото и подача для объявления. Хранится как идея (brief) этого объявления: по ней потом рисуется баннер."""
    biz = _own(conn, user, business_id)
    variants, _count, _pool = _ad_variants(conn, biz)
    if not 0 <= index < len(variants):
        raise HTTPException(400, detail={"message": "Такого объявления нет."})
    v = variants[index]
    usable = {r["id"] for r in usable_photos(conn, biz)}
    if data.asset_id is not None and data.asset_id not in usable:
        raise HTTPException(400, detail={"message": "Это фото нельзя использовать в рекламе."})
    if data.ref_id is not None and data.ref_id not in refs.REFS:
        raise HTTPException(400, detail={"message": "Такой подачи нет."})
    photos, _logo = media_typed(conn, biz)
    brief = _brief_for(conn, business_id, v)
    no_photo = bool(brief["no_photo"]) if brief else False
    if data.no_photo is not None:
        no_photo = data.no_photo
    if data.asset_id is not None or data.look is not None:   # выбрал фото или оформление для фото: возвращаемся к баннеру из фото
        no_photo = False
    asset_id = data.asset_id if data.asset_id is not None else (brief["asset_id"] if brief else None)
    if asset_id is None and photos and not no_photo:
        asset_id = getattr(photo_for(photos, index)[1], "asset_id", None)
    look, ref_id = (brief["look"] if brief else index % len(fal.PHOTO_LOOKS)), (brief["ref_id"] if brief else None)
    if data.ref_id is not None:
        ref_id = data.ref_id
    elif data.look is not None:
        look, ref_id = data.look, None
    _apply_style(conn, biz, v, brief, asset_id, look, ref_id, no_photo, chosen=True)
    conn.commit()
    return {"ok": True}


class AdKey(BaseModel):
    title: str = Field(max_length=200)
    text: str = Field(max_length=400)


class OrderIn(BaseModel):
    ads: list[AdKey] = Field(max_length=40)


@router.put("/ads/order")
def ads_order(business_id: int, data: OrderIn, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Порядок объявлений, который владелец задал перетаскиванием: первые в списке идут в показ, остальные запасные.
    Шаблонный текст без записи при этом сохраняется (он нужен, чтобы запомнить место)."""
    biz = _own(conn, user, business_id)
    variants, _count, _pool = _ad_variants(conn, biz)
    current = {(v["title"], v["text"]): v for v in variants}
    asked = [(a.title, a.text) for a in data.ads]
    if len(set(asked)) != len(asked) or not set(asked) <= set(current):
        raise HTTPException(400, detail={"message": "Список объявлений изменился. Обновите страницу и попробуйте ещё раз."})
    order = asked + [k for k in current if k not in set(asked)]   # объявления, которых нет в запросе, остаются в конце в прежнем порядке
    for place, key in enumerate(order):
        v = current[key]
        if v["creative_id"]:
            conn.execute("UPDATE creatives SET position = ? WHERE id = ?", (place, v["creative_id"]))
        else:
            conn.execute("INSERT INTO creatives (business_id, kind, title, body, angle, source, status, position) VALUES (?, 'text', ?, ?, ?, 'template', 'approved', ?)",
                         (business_id, v["title"], v["text"], v.get("angle", ""), place))
    conn.commit()
    return {"ok": True}


class LiveIn(BaseModel):
    count: int | None = Field(default=None, ge=1, le=MAX_VARIANTS)   # None: вернуть число, которое подходит бюджету


@router.put("/ads/live")
def ads_live(business_id: int, data: LiveIn, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Сколько объявлений из списка идёт в рекламу (остальные запасные). Владелец может взять меньше или больше, чем подходит бюджету."""
    _own(conn, user, business_id)
    conn.execute("UPDATE businesses SET ads_live = ? WHERE id = ?", (data.count, business_id))
    conn.commit()
    return {"ok": True}


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
    if row["kind"] == "text" and (title, body) != (row["title"], row["body"]):   # идея и её фото с подачей остаются с объявлением, а промпт получает новый заголовок
        for b in conn.execute("SELECT id, prompt FROM briefs WHERE business_id = ? AND title = ? AND body = ?", (business_id, row["title"], row["body"])).fetchall():
            conn.execute("UPDATE briefs SET title = ?, body = ?, prompt = ? WHERE id = ?", (title, body, (b["prompt"] or "").replace(row["title"], title), b["id"]))
    conn.commit()
    return _row(conn.execute("SELECT * FROM creatives WHERE id = ?", (creative_id,)).fetchone(), business_id)


@router.post("/{creative_id}/restore")
def restore_banner(business_id: int, creative_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Вернуть прошлый баннер на объявление. Копий не делаем: выбранная версия просто становится показываемой, бесплатно."""
    _own(conn, user, business_id)
    row = _own_creative(conn, business_id, creative_id)
    asset = conn.execute("SELECT stored_name FROM business_assets WHERE id = ?", (row["asset_id"],)).fetchone() if row["asset_id"] else None
    if row["kind"] != "ai_banner" or row["status"] not in ("draft", "approved") or asset is None:
        raise HTTPException(400, detail={"message": "Вернуть можно только готовый баннер."})
    if not _asset_path(business_id, asset["stored_name"]).is_file():
        raise HTTPException(400, detail={"message": "Файл этого баннера уже удалён."})
    newest = _ai_row(conn, business_id, {"title": row["title"], "text": row["body"]})
    if newest is not None and newest["status"] == "working":
        raise HTTPException(400, detail={"message": "Сейчас рисуется новая версия: дождитесь её, потом можно вернуть любую прежнюю."})
    conn.execute("UPDATE creatives SET picked_at = strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE id = ?", (creative_id,))
    conn.commit()
    return {"restored": True}


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
