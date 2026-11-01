"""Стратег: несколько разных идей объявлений для A/B-теста (гипотеза, аудитория, текст, фото и оформление баннера)."""
import asyncio
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.assets import PHOTO_TAGS
from app.config import get_settings
from app.creative import fal, refs
from app.creative.angles import effective_description, label as angle_label, profile
from app.creative.compose import find_price
from app.creative.strategist import LOOKS, plan_briefs
from app.creative.texts_ai import facts_of
from app.db import get_db
from app.deps import creative_transport, current_user
from app.direct.launch import usable_photos
from app.direct.texts import check, unsupported_claims
from app.routes.businesses import _own
from app.routes.creatives import _ad_variants, _charge, _need_fal

router = APIRouter(prefix="/api/businesses/{business_id}/briefs", tags=["briefs"])


class GenerateIn(BaseModel):
    count: int = Field(default=4, ge=2, le=5)


class BriefIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(default="", max_length=200)
    prompt: str = Field(default="", max_length=4000)


def _public(conn, biz, r: sqlite3.Row, variants: list[dict]) -> dict:
    asset = conn.execute("SELECT id FROM business_assets WHERE id = ?", (r["asset_id"],)).fetchone() if r["asset_id"] else None
    index = next((i for i, v in enumerate(variants) if v["title"] == r["title"] and v["text"] == r["body"]), None)
    return {
        "id": r["id"], "hypothesis": r["hypothesis"], "audience": r["audience"], "angle": r["angle"], "angle_label": angle_label(r["angle"]),
        "title": r["title"], "text": r["body"], "look": LOOKS[r["look"] % len(LOOKS)], "prompt": r["prompt"], "status": r["status"],
        "inspiration": refs.note(r["ref_id"]),   # какой приём референса взят за основу (картинку референса пользователю не показываем)
        "photo_url": f"/api/businesses/{biz['id']}/assets/{asset['id']}/file" if asset else "",
        "ad_index": index,   # номер объявления, если идея уже в рекламе (и помещается в бюджет)
    }


def _own_brief(conn, business_id: int, brief_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM briefs WHERE id = ? AND business_id = ?", (brief_id, business_id)).fetchone()
    if row is None:
        raise HTTPException(404, detail={"message": "Такой идеи нет."})
    return row


@router.get("")
def list_briefs(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    biz = _own(conn, user, business_id)
    variants, _count, _pool = _ad_variants(conn, biz)
    rows = conn.execute("SELECT * FROM briefs WHERE business_id = ? ORDER BY id", (business_id,)).fetchall()
    return {"briefs": [_public(conn, biz, r, variants) for r in rows], "price_usd": fal.COST_CENTS["strategist"] / 100}


@router.post("/generate")
def generate(business_id: int, data: GenerateIn, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
             transport=Depends(creative_transport)):
    """Стратег придумывает новые идеи. Прежние идеи, которые вы не взяли в рекламу, заменяются новыми."""
    biz = _own(conn, user, business_id)
    key = _need_fal()
    rows = usable_photos(conn, biz)
    photos = [{"asset_id": r["id"], "label": PHOTO_TAGS.get(r["ai_type"] or r["tag"], "Фото"), "score": r["ai_score"], "note": (r["ai_note"] or "")[:160]}
              for r in rows]
    _charge(conn, business_id, "strategist")
    found = asyncio.run(plan_briefs(biz, photos, data.count, key, get_settings().fal_text_model, transport))
    if not found:
        conn.execute("DELETE FROM generations WHERE id = (SELECT MAX(id) FROM generations WHERE business_id = ? AND kind = 'strategist')", (business_id,))
        conn.commit()
        raise HTTPException(502, detail={"message": "Стратег не предложил подходящих идей. Попробуйте ещё раз."})
    cta = profile(biz)["cta"]
    conn.execute("DELETE FROM briefs WHERE business_id = ? AND status = 'idea'", (business_id,))
    for b in found:
        asset_id = photos[b["photo"]]["asset_id"] if photos and b["photo"] is not None else None
        price = find_price(b["title"], b["text"], fallback=effective_description(biz))
        ref_id = b["ref"] if asset_id else None
        if not asset_id:
            prompt = ""
        elif ref_id:   # по мотивам референса: фото владельца плюс референс как вдохновение
            prompt = fal.reference_banner_prompt(biz, b["title"], price, cta, b["direction"] or f"take inspiration from this idea: {refs.note(ref_id)}")
        else:
            prompt = fal.photo_banner_prompt(biz, b["title"], price, cta, b["look"], b["mood"])
        conn.execute("INSERT INTO briefs (business_id, hypothesis, audience, angle, title, body, asset_id, look, prompt, ref_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (business_id, b["hypothesis"], b["audience"], b["angle"], b["title"], b["text"], asset_id, b["look"], prompt, ref_id))
    conn.commit()
    return list_briefs(business_id, user, conn)


@router.put("/{brief_id}")
def edit(business_id: int, brief_id: int, data: BriefIn, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    biz = _own(conn, user, business_id)
    row = _own_brief(conn, business_id, brief_id)
    if row["status"] == "used":
        raise HTTPException(400, detail={"message": "Эта идея уже в рекламе: поправьте текст на карточке объявления."})
    title, body = data.title.strip(), data.body.strip()
    problems = check(title, body)
    if problems:
        raise HTTPException(400, detail={"message": " ".join(problems)})
    claims = unsupported_claims(f"{title} {body}", facts_of(biz))
    if claims:
        raise HTTPException(400, detail={"message": "Этого нет в описании бизнеса, обещать нельзя: " + ", ".join(claims) + "."})
    conn.execute("UPDATE briefs SET title = ?, body = ?, prompt = ? WHERE id = ?", (title, body, data.prompt.strip(), brief_id))
    conn.commit()
    return {"ok": True}


@router.post("/{brief_id}/use")
def use(business_id: int, brief_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Взять идею в рекламу: её текст становится одобренным, а баннер к нему будет делаться по фото и оформлению идеи."""
    biz = _own(conn, user, business_id)
    row = _own_brief(conn, business_id, brief_id)
    existing = conn.execute("SELECT id FROM creatives WHERE business_id = ? AND kind = 'text' AND title = ? AND body = ? ORDER BY id DESC LIMIT 1",
                            (business_id, row["title"], row["body"])).fetchone()
    if existing:
        conn.execute("UPDATE creatives SET status = 'approved' WHERE id = ?", (existing["id"],))
    else:
        conn.execute("INSERT INTO creatives (business_id, kind, title, body, source, angle, status) VALUES (?, 'text', ?, ?, 'claude', ?, 'approved')",
                     (business_id, row["title"], row["body"], row["angle"]))
    conn.execute("UPDATE briefs SET status = 'used' WHERE id = ?", (brief_id,))
    conn.commit()
    variants, _count, _pool = _ad_variants(conn, biz)
    return _public(conn, biz, _own_brief(conn, business_id, brief_id), variants)


@router.post("/{brief_id}/unuse")
def unuse(business_id: int, brief_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Снять идею с рекламы: она снова просто идея, а её текст больше не одобрен."""
    biz = _own(conn, user, business_id)
    row = _own_brief(conn, business_id, brief_id)
    conn.execute("UPDATE creatives SET status = 'rejected' WHERE business_id = ? AND kind = 'text' AND title = ? AND body = ?",
                 (business_id, row["title"], row["body"]))
    conn.execute("UPDATE briefs SET status = 'idea' WHERE id = ?", (brief_id,))
    conn.commit()
    variants, _count, _pool = _ad_variants(conn, biz)
    return _public(conn, biz, _own_brief(conn, business_id, brief_id), variants)


@router.delete("/{brief_id}")
def drop(business_id: int, brief_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    _own(conn, user, business_id)
    _own_brief(conn, business_id, brief_id)
    conn.execute("DELETE FROM briefs WHERE id = ?", (brief_id,))
    conn.commit()
    return {"ok": True}
