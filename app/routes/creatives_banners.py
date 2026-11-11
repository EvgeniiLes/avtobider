"""Баннеры, видео и проверка фото объявлений (тексты и список объявлений: creatives.py, общие помощники живут там же)."""
import asyncio
import base64
import hashlib
import json
import sqlite3
import time
from io import BytesIO
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from PIL import Image
from pydantic import BaseModel, Field

from app.config import get_settings
from app.creative import fal, motion, refs
from app.creative.angles import profile
from app.assets import PHOTO_TAGS
from app.creative.compose import add_logo, find_price, prepare_for_direct, style_for
from app.creative.art import art_direction, layout_for, palette_hint, smart_placement
from app.creative.texts_ai import (check_photo)
from app.db import get_db
from app.deps import creative_transport, current_user
from app.direct.launch import banner_layout, logo_for, media_typed, photo_for, render_banner
from app.routes.businesses import _asset_path, _own


from app.routes.creatives import (_ad_variants, _ai_row, _auto_style, _brief_for, _bytes_url, _charge, _extras, _headline, _logo_info, _logo_ratio, _need_fal, _other_scenes, _own_creative, _ready_row, _screenshot, _used_styles, _wants_auto_ref)

router = APIRouter(prefix="/api/businesses/{business_id}/creatives", tags=["creatives"])


def _submit_banner(conn, business_id: int, v: dict, meta: dict, prompt: str, images: list[str], key: str, transport, models: list[dict] | None) -> None:
    """Отправляет задание генератору и записывает баннер. Обычный заказ: одна модель из настроек. Тест: то же задание каждой модели из списка,
    баннеры пишутся отдельным видом (test_banner), чтобы не подменить настоящий баннер объявления."""
    s = get_settings()
    for target in models or [None]:
        if target is not None and not images and not target.get("text_only"):
            raise fal.FalError("Для теста нужен референс или фото: без них моделям нечего сравнивать.")
        model = target["id"] if target else s.fal_banner_model
        job = asyncio.run(fal.start_edit(prompt, images, key, model, transport)) if images else asyncio.run(fal.start_image(prompt, key, model, transport))
        saved = dict(meta)
        if target:
            saved.update({"test": True, "model": target["id"], "model_label": target["label"], "run": target["run"]})
        conn.execute("INSERT INTO creatives (business_id, kind, title, body, prompt, status, source, external_id, created_at) "   # дата с долями секунды: сравнивается с датой возврата версии
                     "VALUES (?, ?, ?, ?, ?, 'working', 'fal', ?, strftime('%Y-%m-%d %H:%M:%f', 'now'))",
                     (business_id, "test_banner" if target else "ai_banner", v["title"], v["text"], json.dumps(saved, ensure_ascii=False), json.dumps(job)))


def _order_banner(conn, biz, variants: list[dict], index: int, key: str, transport, others: list[str], custom_prompt: str = "",
                  models: list[dict] | None = None, scheme: str = "") -> str:
    """Заказывает один ИИ-баннер. Есть фото владельца: модель делает баннер из этого фото (оформление у каждого объявления своё).
    Фото нет: арт-директор придумывает сцену, художник рисует с нуля. custom_prompt: промпт, который владелец поправил сам. Возвращает описание.
    models: тест нейросетей, одно и то же задание (промпт и картинки) уходит каждой модели из списка."""
    business_id = biz["id"]
    v = variants[index]
    s = get_settings()
    price = find_price(v["title"], v["text"], fallback=biz["description"])
    cta = profile(biz)["cta"]
    expected = [_headline(v), cta] + ([price] if price else [])   # на баннере только заголовок, кнопка и цена: подписи с названием нет
    extras = _extras()   # экран со скриншотом и логотип в слоте: пока выключены, сначала добиваемся хорошего базового баннера
    photos, _logo = media_typed(conn, biz)
    brief = _brief_for(conn, business_id, v)   # идея стратега или выбор владельца в редакторе: фото, оформление, референс, промпт
    no_photo = bool(brief and brief["no_photo"])   # владелец выбрал «без фото»: ИИ рисует всю сцену сам
    free = scheme == "free"   # схема без референса и без фото: композицию даёт библиотека проверенных раскладок, а не картинка
    if not free and photos and not extras and not no_photo:   # главный путь: настоящее фото владельца отдаётся модели вместе с промптом про оформление
        varied = ""
        if _wants_auto_ref(brief, index) and not custom_prompt:   # подача не выбрана: берём приём из ниши, у соседних объявлений разные
            brief = _auto_style(conn, biz, v, index, getattr(photo_for(photos, index)[1], "asset_id", None), brief) or brief
        # повторный заказ подачу не меняет: референс остаётся тем же, пока владелец сам не выберет другой (результат всё равно получается новым)
        kind, data = photo_for(photos, index)
        if brief and brief["asset_id"]:
            kind, data = next(((k, d) for k, d in photos if getattr(d, "asset_id", None) == brief["asset_id"]), (kind, data))
        look_no = brief["look"] if brief else index
        look = fal.PHOTO_LOOKS[look_no % len(fal.PHOTO_LOOKS)][0]
        prompt = custom_prompt or (brief["prompt"] if brief and brief["prompt"] else fal.photo_banner_prompt(biz, _headline(v), price, cta, look_no))
        ref_bytes = refs.load(brief["ref_id"]) if brief and brief["ref_id"] else None   # референс-вдохновение идёт второй картинкой
        images = [_bytes_url(data, 1280)] + ([_bytes_url(ref_bytes, 900)] if ref_bytes else [])
        if ref_bytes:
            _charge(conn, business_id, "ref_extra")
            layout = asyncio.run(refs.layout_text(brief["ref_id"], key, s.fal_vision_model, transport))   # раскладка референса словами: к картинке добавляется подробное описание
            if layout and not custom_prompt and "Reference layout in words" not in prompt:
                prompt += f" Reference layout in words (follow it for the design, put the business photo into it): {layout}"
        placement = ""
        use_smart = scheme == "smart" or (not scheme and models is None and bool(ref_bytes) and not custom_prompt)   # есть фото и референс: арт-директор вписывает фото
        if use_smart:   # «умная схема»: арт-директор смотрит на фото и референс вместе и пишет, как вписать фото
            if not ref_bytes:
                raise fal.FalError("Умная схема вписывает фото в раскладку референса: выберите референс.")
            _charge(conn, business_id, "strategist")
            placement = asyncio.run(smart_placement(biz, data, ref_bytes, refs.note(brief["ref_id"]), key, s.fal_vision_model, transport))
            if placement:
                prompt += f" Art director's placement instructions for the business photo (follow them): {placement}"
        what = f"фото «{PHOTO_TAGS.get(kind, kind)}», " + (f"по мотивам референса {brief['ref_id']}" if ref_bytes else f"оформление: {look}")
        meta = {"scheme": scheme or ("smart" if use_smart else "direct"), "placement": placement, "expect": expected, "caption": fal.caption_for(biz), "logo": False, "screen": False, "mode": "photo", "concept": kind, "look": look,
                "look_no": look_no % len(fal.PHOTO_LOOKS), "photo_id": getattr(data, "asset_id", None), "varied": varied,
                "ref": brief["ref_id"] if ref_bytes else None,
                "audience": brief["audience"] if brief else "", "hypothesis": brief["hypothesis"] if brief else "", "scene": what,
                "logo_reason": "", "prompt_text": prompt,
                "brief": ("ВАЖНО: основа баннера настоящее фото владельца, его нужно принимать как есть. "
                          + (f"Баннер сделан по мотивам приёма «{refs.note(brief['ref_id'])}»: цвета и подача определяются этим приёмом, а не словами «светлое» "
                             "или «тёмное»; огромный заголовок или слово, которое частично перекрывается объектом, это задумка, а не недостаток. "
                             if ref_bytes else f"Оформление: {look}. ") + f"{biz['kind']}.")}
        _submit_banner(conn, business_id, v, meta, prompt, images, key, transport, models)
        return what
    # баннер с нуля: фото нет или владелец выбрал «без фото». Сцену придумывает арт-директор, подача по референсу идёт словами
    if scheme == "smart":
        raise fal.FalError("Умная схема вписывает ваше фото в раскладку референса: выберите фото (не «Без фото») и референс.")
    varied = ""
    if not free and _wants_auto_ref(brief, index) and not extras and not custom_prompt:   # подача не выбрана: берём приём из ниши
        brief = _auto_style(conn, biz, v, index, None, brief) or brief
    ref_id = None if free else (brief["ref_id"] if brief and not extras else None)
    if not free and not scheme and models is None and not ref_id and not custom_prompt:   # ни фото, ни референса: композиция из библиотеки (схема 3)
        free = True
    neighbours = set()
    if free:   # раскладки других объявлений этого бизнеса: у соседних баннеров они должны отличаться
        for r in conn.execute("SELECT prompt FROM creatives WHERE business_id = ? AND kind = 'ai_banner' AND status IN ('draft', 'approved', 'working') "
                              "AND NOT (title = ? AND body = ?)", (business_id, v["title"], v["text"])):
            try:
                neighbours.add(json.loads(r["prompt"] or "{}").get("layout") or "")
            except ValueError:
                pass
    layout = layout_for(index, _headline(v), biz["kind"], bool(find_price(v["title"], v["text"], fallback=biz["description"])), neighbours - {""}) if free else None
    done_before = len(_used_styles(conn, business_id, v, scratch=True))   # от этого зависит, какое из стандартных оформлений возьмём
    earlier = [json.loads(r["prompt"] or "{}") for r in conn.execute(
        "SELECT prompt FROM creatives WHERE business_id = ? AND kind = 'ai_banner' AND title = ? AND body = ? AND status IN ('draft', 'approved') "
        "ORDER BY id DESC LIMIT 6", (business_id, v["title"], v["text"]))]
    own_scenes = [m.get("scene", "")[:160] for m in earlier if m.get("mode") != "photo"][:3]   # прежние сцены этого же объявления: не повторять
    logo = logo_for(conn, biz) if extras else None
    screen = extras and _screenshot(conn, business_id) is not None
    art = asyncio.run(art_direction(biz, v, index, [x for x in others + own_scenes if x][-8:], _logo_info(conn, biz, logo), screen, key, s.fal_text_model, transport,
                                    refs.note(ref_id) if ref_id else "", refs.load(ref_id) if ref_id else None, layout))
    use_logo = bool(art["logo"]) and logo is not None
    # референс уходит модели картинкой: по одной строке слов сходства почти нет, а картинка задаёт композицию, цвет и шрифт
    ref_bytes = refs.load(ref_id) if ref_id else None
    if ref_bytes:
        _charge(conn, business_id, "ref_extra")
        direction = (f"the attached image is ONLY a style inspiration (the idea of its composition, color mood, typography weight and feel: {refs.note(ref_id)}). "
                     "Follow that layout idea closely, but make a new original banner with the scene above: do not copy or reuse its text, logo, products, people or characters, "
                     "and do not recreate its small print, captions, lists, badges, footer or contact lines: the only text on the banner is the text given below")
    elif free:   # без референса композицию задаёт проверенная раскладка, цвета берутся у бизнеса
        direction = (f"Composition ({layout[1]}): {layout[2]}. {palette_hint(biz)} Follow this composition closely: it is what makes the banner look like a professional ad")
    else:
        direction = (f"{refs.note(ref_id)}. Take this advertising idea only as inspiration for the layout, color mood and type weight; "
                     "make an original design with your own scene and do not reuse any text, logo or character from it") if ref_id else ""
    prompt = custom_prompt or fal.banner_prompt(biz, _headline(v), price, cta, logo_corner=use_logo, index=index + done_before, screen=screen,
                                                logo_ratio=_logo_ratio(logo), scene=art["scene"], direction=direction)
    meta = {"scheme": scheme or ("free" if free else "direct"), "layout": layout[1] if layout else "",
            "expect": expected, "caption": fal.caption_for(biz), "logo": use_logo, "screen": screen, "concept": art["concept"], "audience": art["audience"], "scene": art["scene"],
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
    _submit_banner(conn, business_id, v, meta, prompt, [_bytes_url(ref_bytes, 900)] if ref_bytes else [], key, transport, models)
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
        raise HTTPException(502, detail={"message": e.message}) from e
    conn.commit()
    row = _ai_row(conn, business_id, variants[index])
    varied = json.loads(row["prompt"] or "{}").get("varied", "") if row is not None else ""
    return {"started": True, **({"varied": varied} if varied else {})}   # varied: какую подачу взяли вместо прежней


class ModelTestIn(BaseModel):
    scheme: Literal["direct", "smart", "free"] = "direct"   # direct: фото и референс прямо в задание; smart: арт-директор сначала пишет, как вписать фото; free: без фото и без референса


@router.post("/ads/{index}/model-test")
def model_test(business_id: int, index: int, data: ModelTestIn | None = None, user: sqlite3.Row = Depends(current_user),
               conn: sqlite3.Connection = Depends(get_db), transport=Depends(creative_transport)):
    """Вкладка «Объявления (тест)»: одно и то же объявление (текст, фото, референс, задание) рисуют три модели, чтобы сравнить их рядом.
    Схем две: прямая (как обычно) и «умная» (арт-директор смотрит на фото и референс вместе и пишет, как вписать фото)."""
    scheme = data.scheme if data else "direct"
    biz = _own(conn, user, business_id)
    key = _need_fal()
    variants, _count, _pool = _ad_variants(conn, biz)
    if not 0 <= index < len(variants):
        raise HTTPException(400, detail={"message": "Такого объявления нет."})
    v = variants[index]
    if conn.execute("SELECT 1 FROM creatives WHERE business_id = ? AND kind = 'test_banner' AND title = ? AND body = ? AND status = 'working'",
                    (business_id, v["title"], v["text"])).fetchone():
        raise HTTPException(400, detail={"message": "Тест для этого объявления ещё идёт. Подождите минуту."})
    run = str(int(time.time()))
    models = [{**m, "run": run} for m in (fal.TEST_MODELS_FREE if scheme == "free" else fal.TEST_MODELS)]
    for _ in models:
        _charge(conn, business_id, "ai_banner")
    try:
        _order_banner(conn, biz, variants, index, key, transport, _other_scenes(conn, business_id, variants, index), "", models, scheme)
    except fal.FalError as e:
        conn.rollback()
        raise HTTPException(400 if "выберите" in e.message else 502, detail={"message": e.message}) from e
    conn.commit()
    return {"started": len(models), "run": run, "scheme": scheme}


@router.post("/{creative_id}/use-test")
def use_test_banner(business_id: int, creative_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Взять баннер из теста на объявление: он становится обычным и встаёт на карточку вместо прежнего (прежний остаётся в версиях)."""
    _own(conn, user, business_id)
    row = _own_creative(conn, business_id, creative_id)
    if row["kind"] != "test_banner" or row["status"] != "draft" or not row["asset_id"]:
        raise HTTPException(400, detail={"message": "Этот баннер нельзя взять на объявление."})
    conn.execute("UPDATE creatives SET kind = 'ai_banner', picked_at = strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE id = ?", (creative_id,))
    conn.commit()
    return {"ok": True}


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
            raise HTTPException(502, detail={"message": e.message}) from e
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
        cache_key = ("ai", business_id, asset["stored_name"], row["id"])
        picture = _BANNERS.get(cache_key)
        if picture is None:   # обработка картинки занимает время, а страницу открывают часто: готовый результат держим в памяти
            picture = prepare_for_direct(_asset_path(business_id, asset["stored_name"]).read_bytes())
            made = json.loads(row["prompt"] or "{}")
            if not made.get("baked") and "concept" not in made:  # только старые баннеры: у новых решение про логотип принято при заказе
                picture = add_logo(picture, logo)
            if len(_BANNERS) >= 48:
                _BANNERS.pop(next(iter(_BANNERS)))
            _BANNERS[cache_key] = picture
        return Response(picture, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
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


def _banner_for_motion(conn, biz, index: int):
    """Готовый баннер объявления: (вариант, строка заказа, байты картинки для видео). 400, если баннера ещё нет."""
    variants, _count, _pool = _ad_variants(conn, biz)
    if not 0 <= index < len(variants):
        raise HTTPException(404, detail={"message": "Такого объявления нет."})
    row = _ready_row(conn, biz["id"], variants[index])
    asset = conn.execute("SELECT * FROM business_assets WHERE id = ?", (row["asset_id"],)).fetchone() if row else None
    if row is None or asset is None:
        raise HTTPException(400, detail={"message": "Видео делается из готового баннера. Сначала нарисуйте баннер для этого объявления."})
    picture = prepare_for_direct(_asset_path(biz["id"], asset["stored_name"]).read_bytes())
    return variants[index], row, picture


@router.post("/ads/{index}/motion-ideas")
def motion_ideas(business_id: int, index: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
                 transport=Depends(creative_transport)):
    """Несколько готовых идей, как оживить баннер именно этого объявления: владелец выбирает одну или пишет свою."""
    biz = _own(conn, user, business_id)
    variant, row, _picture = _banner_for_motion(conn, biz, index)
    s = get_settings()
    try:
        scene = json.loads(row["prompt"] or "{}").get("scene", "")
    except ValueError:
        scene = ""
    found, from_ai = asyncio.run(motion.ideas(biz, variant["title"], scene, s.fal_key, s.fal_text_model, transport))
    if from_ai:
        _charge(conn, business_id, "check")
        conn.commit()
    return {"ideas": found, "ai": from_ai}


class AdVideoIn(BaseModel):
    prompt: str = Field(default="", max_length=400)


@router.post("/ads/{index}/video")
def ad_video(business_id: int, index: int, data: AdVideoIn, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
             transport=Depends(creative_transport)):
    """Оживляет готовый баннер объявления в короткое видео. Движение: выбранная идея или текст владельца; текст и логотип на баннере страхуем."""
    biz = _own(conn, user, business_id)
    key = _need_fal()
    variant, _row_, picture = _banner_for_motion(conn, biz, index)
    wish = asyncio.run(motion.to_english(data.prompt, key, get_settings().fal_text_model, transport))   # своё пожелание по-русски переводим для видеомодели
    prompt = motion.final_prompt(wish)
    _charge(conn, business_id, "video")
    try:
        job = asyncio.run(fal.start_video(_bytes_url(picture, 1280), prompt, key, get_settings().fal_video_model, transport))
    except fal.FalError as e:
        conn.commit()
        raise HTTPException(502, detail={"message": e.message}) from e
    conn.execute("INSERT INTO creatives (business_id, kind, title, body, prompt, status, source, external_id) VALUES (?, 'video', ?, ?, ?, 'working', 'fal', ?)",
                 (business_id, variant["title"], variant["text"], (data.prompt.strip() or "Плавное движение")[:400], json.dumps(job)))
    conn.commit()
    return {"started": 1}

@router.post("/ads/{index}/accept-rejected")
def accept_rejected_banner(business_id: int, index: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Владелец видит забракованный проверкой баннер и считает его хорошим: принимаем его на объявление без новой оплаты."""
    biz = _own(conn, user, business_id)
    variants, _count, _pool = _ad_variants(conn, biz)
    row = _ai_row(conn, business_id, variants[index]) if 0 <= index < len(variants) else None
    if row is None or row["status"] != "failed" or not row["asset_id"]:
        raise HTTPException(400, detail={"message": "Забракованного баннера у этого объявления нет."})
    conn.execute("UPDATE creatives SET status = 'draft', error = '', picked_at = strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE id = ?", (row["id"],))
    conn.commit()
    return {"accepted": True}


@router.get("/ads/{index}/rejected.jpg")
def ad_rejected_banner(business_id: int, index: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Последний баннер объявления, который не принят проверкой: чтобы владелец видел, что именно не так."""
    biz = _own(conn, user, business_id)
    variants, _count, _pool = _ad_variants(conn, biz)
    row = _ai_row(conn, business_id, variants[index]) if 0 <= index < len(variants) else None
    asset = conn.execute("SELECT stored_name FROM business_assets WHERE id = ?", (row["asset_id"],)).fetchone() if row is not None and row["status"] == "failed" and row["asset_id"] else None
    if asset is None:
        raise HTTPException(404, detail={"message": "Забракованного баннера нет."})
    return Response(_asset_path(business_id, asset["stored_name"]).read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=60"})


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
            result = asyncio.run(check_photo(_data_url(_asset_path(business_id, r["stored_name"])), key, s.fal_vision_model, transport,
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


class ImagesIn(BaseModel):
    count: int = Field(default=5, ge=1, le=5)
    idea: str = Field(default="", max_length=300)


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
            raise HTTPException(502, detail={"message": e.message}) from e
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
        raise HTTPException(502, detail={"message": e.message}) from e
    conn.execute("INSERT INTO creatives (business_id, kind, prompt, status, source, external_id) VALUES (?, 'video', ?, 'working', 'fal', ?)",
                 (business_id, f"из картинки #{image['id']}", json.dumps(job)))
    conn.commit()
    return {"started": 1}
