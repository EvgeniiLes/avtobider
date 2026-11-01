"""Запуск рекламы бизнеса в Яндекс Директе: план, создание кампаний, отправка на проверку, синхронизация.

Устройство: одна кампания с одним недельным лимитом, внутри одна группа (автотаргетинг включается сам, ключевые фразы
не нужны) и 2-3 объявления-варианта. Варианты показываются в одних и тех же аукционах, так что сравнение честное, а ставки
подбирает сам Яндекс: сначала по кликам, а когда набирается 10 обращений в неделю, по обращениям (см. app/engine/cycle.py).
Раньше на каждый вариант была своя кампания; на малом бюджете у такой кампании не набиралось данных ни для сравнения,
ни для обучения стратегии Яндекса (ему нужно не меньше 10 конверсий в неделю на кампанию).
"""
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from app.creative.compose import style_for
from app.creative.angles import is_nationwide
from app.direct.client import RUSSIA_REGION_ID, DirectClient, DirectError
from app.direct.negatives import negatives_for
from app.direct.texts import check, draft_variants
from app.engine.util import EngineError, MICRO, day_add, plural, rub

MAX_VARIANTS = 6   # столько вариантов готовим и показываем в редакторе; в кампанию входят первые MAX_ADS, остальные запасные
MAX_ADS = 3        # объявлений в кампании: на малом бюджете больше не успеют набрать клики для сравнения
START_SHARE = 0.7   # какую часть доступного бюджета отдаём кампании сразу, остальное про запас для роста
RESERVE = 0.10      # часть недельного бюджета, которую не раздаём вообще
MIN_WEEKLY = 300 * MICRO  # минимум Директа на кампанию; точное значение берём из справочника

STATE_LABELS = {
    "draft": "Черновик",
    "moderation": "На проверке в Яндексе",
    "rejected": "Не прошло проверку",
    "unpaid": "Нужно пополнить счёт",
    "active": "Идёт показ",
    "paused": "Остановлено",
}


@dataclass
class Plan:
    count: int            # сколько объявлений войдёт в кампанию
    campaign_micro: int   # недельный лимит единственной кампании
    variants: list[dict]
    region_id: int
    city: str
    href: str
    business_id: int | None
    warnings: list[str]
    counter_ids: list[int] | None = None
    own_count: int = 0
    negatives: list[str] | None = None


def _maps_id(biz) -> int | None:
    from app.routes.businesses import maps_org_id  # импорт здесь, чтобы не было круга
    value = maps_org_id(biz["maps_url"])
    return int(value) if value else None


def text_candidates(conn: sqlite3.Connection, biz) -> list[dict]:
    """Тексты для объявлений по порядку: сначала одобренные владельцем, затем предложенные ИИ (по одному на угол подачи), потом шаблоны.
    Отклонённые не берём. Так новые тексты от ИИ попадают в рекламу, не дожидаясь одобрения, а владелец может их убрать."""
    rows = conn.execute(
        "SELECT id, title, body, angle, status, source FROM creatives WHERE business_id = ? AND kind = 'text' "
        "AND status IN ('approved', 'draft') ORDER BY id", (biz["id"],)).fetchall()
    chosen = [{"id": r["id"], "title": r["title"], "text": r["body"], "angle": r["angle"], "status": "approved"} for r in rows if r["status"] == "approved"]
    angles = {c["angle"] for c in chosen if c["angle"]}
    from app.creative.texts_ai import facts_of  # здесь, чтобы не было круга
    from app.direct.texts import unsupported_claims
    facts = facts_of(biz)
    for r in reversed([r for r in rows if r["status"] == "draft" and r["source"] == "claude"]):  # новые сначала
        if r["angle"] and r["angle"] in angles:
            continue
        if unsupported_claims(f"{r['title']} {r['body']}", facts):   # старый черновик со скидкой, когда скидки выключены, в рекламу не идёт
            continue
        angles.add(r["angle"])
        chosen.append({"id": r["id"], "title": r["title"], "text": r["body"], "angle": r["angle"], "status": "draft"})
    return chosen


THREE_ADS_POOL = 3000   # с какого недельного лимита кампании (₽) кладём в неё три объявления
TWO_ADS_POOL = 1500     # и с какого два


def ads_for_budget(pool_micro: int) -> int:
    """Сколько объявлений класть в кампанию: чем меньше денег, тем меньше вариантов, иначе у каждого мало кликов для сравнения."""
    rubles = pool_micro / MICRO
    return MAX_ADS if rubles >= THREE_ADS_POOL else 2 if rubles >= TWO_ADS_POOL else 1


def start_split(biz, min_weekly_micro: int = MIN_WEEKLY) -> tuple[int, int]:
    """Недельный лимит кампании и число объявлений в ней (0 объявлений: денег не хватает даже на минимум Директа)."""
    start_pool = int(int(biz["weekly_budget_micro"] * (1 - RESERVE)) * START_SHARE)
    return start_pool, ads_for_budget(start_pool) if start_pool >= min_weekly_micro else 0


def choose_variants(biz, custom: list[dict] | None, count: int) -> tuple[list[dict], int]:
    """Сначала тексты, которые владелец одобрил; недостающие добираем из шаблонов без повторов. Второе число: сколько своих."""
    variants = [{"title": c["title"], "text": c["text"], "angle": c.get("angle", "")}
                for c in (custom or []) if not check(c["title"], c["text"])][:count]
    own = len(variants)
    for v in draft_variants(biz, MAX_VARIANTS):
        if len(variants) >= count:
            break
        if v["title"].lower() not in {x["title"].lower() for x in variants}:
            variants.append(v)
    return variants, own


def banner_layout(index: int) -> str:
    return "bottom" if index % 2 == 0 else "left"


def render_banner(biz, variant: dict, photo: bytes, logo: bytes | None, index: int) -> bytes:
    """Баннер объявления: предложение, название бизнеса, цена (только названная владельцем) и кнопка «Записаться»."""
    from app.creative.angles import profile
    from app.creative.compose import find_price, make_banner, style_for
    price = find_price(variant["title"], variant["text"], fallback=biz["description"])
    # у каждого объявления свой шаблон (их пять и они непохожи): так варианты по-настоящему различаются для сравнения
    return make_banner(photo, variant["title"], biz["color_main"], logo, banner_layout(index // 5),
                       brand=biz["name"], badge=price, cta=profile(biz)["cta"], style=style_for(index), body=variant["text"])


def plan_launch(biz, region_id: int | None, min_weekly_micro: int = MIN_WEEKLY, custom: list[dict] | None = None) -> Plan:
    """Считает, что будет создано. Если создавать нечего или нельзя, объясняет простыми словами."""
    if not (biz["site_url"] or _maps_id(biz)):
        raise EngineError("Укажите сайт или ссылку на ваш бизнес в Яндекс Картах: без них объявлению некуда вести.", "no_target_link")
    nationwide = is_nationwide(biz)   # онлайн-услуга: показываем по всей России, города нет и искать его в Яндексе не нужно
    if not nationwide and not biz["city"].strip():
        raise EngineError("Укажите город, где показывать рекламу, или отметьте «Показывать по всей России».", "no_city")
    if region_id is None and not nationwide:
        raise EngineError(f"Не нашёл город «{biz['city']}» в Яндексе. Проверьте, как он написан (например, «Москва»).", "no_region")
    if biz["weekly_budget_micro"] <= 0:
        raise EngineError("Укажите бюджет на неделю.", "no_budget")

    start_pool, count = start_split(biz, min_weekly_micro)
    if count < 1:
        need = rub(min_weekly_micro / START_SHARE / (1 - RESERVE))
        raise EngineError(f"Бюджет слишком мал: на одну кампанию Яндекс требует минимум {rub(min_weekly_micro)} в неделю. Нужно хотя бы {need} в неделю.", "budget_too_small")

    variants, own = choose_variants(biz, custom, count)
    warnings = []
    for v in variants:
        warnings += check(v["title"], v["text"])
    if custom and own < min(count, 2):
        warnings.append("Одобрено мало своих вариантов. Для сравнения лучше одобрить хотя бы 2–3 на вкладке «Креативы».")
    if count < MAX_ADS:
        warnings.append(f"При таком бюджете в кампанию войдёт {plural(count, 'объявление', 'объявления', 'объявлений')}: "
                        "на каждое придётся мало кликов. Больше бюджет, больше вариантов для сравнения.")
    from app.direct.advice import budget_advice   # здесь, чтобы не было круга
    advice = budget_advice(biz, biz["description"])
    if advice["level"] == "low":   # хватает на запуск и сравнение, но не на подбор ставок под обращения: владелец должен знать об этом до запуска
        warnings.append(advice["summary"])
    counters = [int(biz["metrika_counter_id"])] if biz["metrika_counter_id"] else None
    return Plan(
        count=count, campaign_micro=start_pool, variants=variants, region_id=RUSSIA_REGION_ID if nationwide else region_id,
        city="вся Россия" if nationwide else biz["city"], href=biz["site_url"], business_id=_maps_id(biz), warnings=warnings, counter_ids=counters,
        own_count=own, negatives=negatives_for(biz["kind"]),
    )


async def create_campaign(client: DirectClient, plan: Plan, name: str, start_date: str) -> dict:
    """Создаёт кампанию, группу и в ней все объявления плана. Если что-то не вышло, удаляет созданное и пробрасывает ошибку."""
    campaign_id = None
    try:
        campaign_id = await client.create_text_campaign(
            name, plan.campaign_micro, start_date, counter_ids=plan.counter_ids, negative_keywords=plan.negatives)
        group_id = await client.create_ad_group(campaign_id, "Основная", [plan.region_id])
        ad_ids = [await client.create_text_ad(group_id, v["title"], v["text"], href=plan.href, business_id=plan.business_id)
                  for v in plan.variants]
    except DirectError:
        if campaign_id:
            try:
                await client.delete_campaigns([campaign_id])
            except DirectError:
                pass
        raise
    return {"campaign_id": campaign_id, "group_id": group_id, "ad_ids": ad_ids}


def save_unit(conn: sqlite3.Connection, biz, account_id: int, campaign_row_id: int, variant: dict, ids: dict,
              weekly_micro: int, today: str, variant_of: int | None = None, style: str = "", photo_type: str = "",
              angle: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO ad_units (campaign_id, business_id, title, body, state, budget_micro, variant_of, last_changed_day, created_day, "
        "external_campaign_id, external_group_id, external_ad_id, style, photo_type, angle) VALUES (?,?,?,?, 'moderation', ?,?,?,?,?,?,?,?,?,?)",
        (campaign_row_id, biz["id"], variant["title"], variant["text"], weekly_micro, variant_of, today, today,
         ids["campaign_id"], ids["group_id"], ids["ad_id"], style, photo_type, angle),
    )
    return cur.lastrowid


def _file(business_id: int, stored_name: str) -> bytes:
    from app.config import get_settings
    return (get_settings().uploads_dir / str(business_id) / stored_name).read_bytes()


class Photo(bytes):
    """Файл фото, который знает свой номер (asset_id) и точку главного объекта (focus): шаблоны режут кадр по ней, а не по центру."""

    def __new__(cls, data: bytes, asset_id: int | None = None, fx: float | None = None, fy: float | None = None):
        obj = super().__new__(cls, data)
        obj.asset_id = asset_id
        obj.focus = (fx, fy) if fx is not None and fy is not None else None
        return obj


TYPE_ORDER = ("interior", "work", "product", "team", "other")  # порядок типов кадра: помещение, работы (руки), товар, люди, прочее


def usable_photos(conn: sqlite3.Connection, biz) -> list[sqlite3.Row]:
    """Фото, из которых можно делать баннеры: выбранные владельцем сгенерированные картинки и загруженные фото.
    Кадры, у которых ИИ нашёл серьёзные проблемы (оценка 1–2), и скриншоты приложения не берём."""
    rows = conn.execute(
        "SELECT a.id, a.stored_name, a.ai_type, a.tag, a.ai_score, a.ai_note, a.ai_fx, a.ai_fy FROM creatives c JOIN business_assets a ON a.id = c.asset_id "
        "WHERE c.business_id = ? AND c.kind = 'image' AND c.status = 'approved' ORDER BY c.id", (biz["id"],)
    ).fetchall() + conn.execute(
        "SELECT id, stored_name, ai_type, tag, ai_score, ai_note, ai_fx, ai_fy FROM business_assets WHERE business_id = ? AND kind = 'photo' AND tag != 'screen' ORDER BY id LIMIT 12", (biz["id"],)
    ).fetchall()
    return [r for r in rows if not 0 < r["ai_score"] < 3]


def media_typed(conn: sqlite3.Connection, biz) -> tuple[list[tuple[str, bytes]], bytes | None]:
    """Картинки для баннеров вместе с типом кадра (помещение, работы…). Тип: по оценке ИИ, иначе по выбору владельца."""
    rows = usable_photos(conn, biz)
    photos = []
    for r in rows:
        try:
            photos.append((r["ai_type"] or r["tag"] or "other", Photo(_file(biz["id"], r["stored_name"]), r["id"], r["ai_fx"], r["ai_fy"])))
        except OSError:
            continue
    return photos, logo_for(conn, biz)


def logo_for(conn: sqlite3.Connection, biz) -> bytes | None:
    """Логотип для баннеров. Если ИИ-проверка нашла в нём серьёзные проблемы (оценка 1–2), на баннеры он не попадает."""
    logo = conn.execute("SELECT stored_name, ai_score FROM business_assets WHERE business_id = ? AND kind = 'logo'", (biz["id"],)).fetchone()
    if logo is None or 0 < logo["ai_score"] < 3:
        return None
    try:
        return _file(biz["id"], logo["stored_name"])
    except OSError:
        return None


def photo_for(typed: list[tuple[str, bytes]], index: int) -> tuple[str, bytes]:
    """Фото для объявления номер index: типы кадра чередуются (помещение, руки, помещение, руки…), внутри типа фото по кругу.
    Так в сравнении участвуют разные по сути варианты, по два на каждый тип."""
    groups: dict[str, list[bytes]] = {}
    for kind, data in typed:
        groups.setdefault(kind if kind in TYPE_ORDER else "other", []).append(data)
    kinds = [k for k in TYPE_ORDER if k in groups]
    kind = kinds[index % len(kinds)]
    pool = groups[kind]
    return kind, pool[(index // len(kinds)) % len(pool)]


def media_for(conn: sqlite3.Connection, biz) -> tuple[list[bytes], bytes | None]:
    """Картинки для баннеров: сначала одобренные владельцем, иначе его загруженные фото. И логотип, если есть."""
    rows = conn.execute(
        "SELECT a.stored_name FROM creatives c JOIN business_assets a ON a.id = c.asset_id "
        "WHERE c.business_id = ? AND c.kind = 'image' AND c.status = 'approved' ORDER BY c.id", (biz["id"],)
    ).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT stored_name FROM business_assets WHERE business_id = ? AND kind = 'photo' ORDER BY id LIMIT 6", (biz["id"],)
        ).fetchall()
    logo = conn.execute("SELECT stored_name FROM business_assets WHERE business_id = ? AND kind = 'logo'", (biz["id"],)).fetchone()
    photos = []
    for r in rows:
        try:
            photos.append(_file(biz["id"], r["stored_name"]))
        except OSError:
            continue
    try:
        return photos, (_file(biz["id"], logo["stored_name"]) if logo else None)
    except OSError:
        return photos, None


def ai_scenes_for(conn: sqlite3.Connection, biz) -> dict[tuple, str]:
    """Сюжет каждого готового ИИ-баннера (ключ как у ai_banners_for): для сравнения сцен в результатах."""
    found: dict[tuple, str] = {}
    for r in conn.execute("SELECT title, body, prompt FROM creatives WHERE business_id = ? AND kind = 'ai_banner' "
                          "AND status IN ('draft', 'approved') ORDER BY COALESCE(picked_at, created_at), id", (biz["id"],)):
        try:
            concept = json.loads(r["prompt"] or "{}").get("concept")
        except ValueError:
            concept = None
        if concept:
            found[(r["title"], r["body"])] = concept
    return found


def ai_banners_for(conn: sqlite3.Connection, biz) -> dict[tuple, bytes]:
    """Готовые ИИ-баннеры (стиль «ИИ-дизайн»), уже проверенные на ошибки в тексте: ключ (заголовок, текст)."""
    from app.creative.compose import add_logo, prepare_for_direct
    found: dict[tuple, bytes] = {}
    logo = logo_for(conn, biz)
    rows = conn.execute(
        "SELECT c.title, c.body, c.prompt, a.stored_name FROM creatives c JOIN business_assets a ON a.id = c.asset_id "
        "WHERE c.business_id = ? AND c.kind = 'ai_banner' AND c.status IN ('draft', 'approved') ORDER BY COALESCE(c.picked_at, c.created_at), c.id", (biz["id"],)).fetchall()
    for r in rows:  # более поздний заказ перекрывает ранний
        try:
            picture = prepare_for_direct(_file(biz["id"], r["stored_name"]))
            try:
                made = json.loads(r["prompt"] or "{}")
                baked = made.get("baked") or "concept" in made   # у новых баннеров решение про логотип принято при заказе
            except ValueError:
                baked = False
            found[(r["title"], r["body"])] = picture if baked else add_logo(picture, logo)  # у новых баннеров логотип уже вшит в макет
        except (OSError, ValueError):
            continue
    return found


async def attach_banners(client: DirectClient, biz, created: list[dict], photos: list[tuple[str, bytes]], logo: bytes | None,
                         ai: dict[tuple, bytes] | None = None) -> list[str]:
    """Собирает баннер под каждое объявление и привязывает к нему. Если не вышло, объявление остаётся без картинки."""
    problems = []
    for i, c in enumerate(created):
        try:
            ready = (ai or {}).get((c["variant"]["title"], c["variant"]["text"]))
            if ready is not None:
                banner = ready  # ИИ-баннер целиком
            elif photos:
                banner = render_banner(biz, c["variant"], photo_for(photos, i)[1], logo, i)
            else:
                continue
            image_hash = await client.upload_image(banner, f"autobider-{biz['id']}-{i + 1}.jpg")
            await client.attach_image(c["ad_id"], image_hash)
            c["image_hash"] = image_hash
        except DirectError as e:
            problems.append(f"«{c['variant']['title']}»: {e.message}")
        except Exception:  # битая картинка не должна ломать запуск
            problems.append(f"«{c['variant']['title']}»: не удалось подготовить картинку")
    return problems


async def launch(client: DirectClient, conn: sqlite3.Connection, biz, account_id: int, plan: Plan, today: str) -> list[int]:
    """Создаёт в кабинете кампанию с объявлениями, добавляет баннеры, отправляет на проверку и записывает в базу."""
    start_date = day_add(today, 1)
    photos, logo = media_typed(conn, biz)
    ai_banners = ai_banners_for(conn, biz)
    ai_scenes = ai_scenes_for(conn, biz)
    ids = await create_campaign(client, plan, f"Автобидер: {biz['name']}", start_date)
    created = [{"ad_id": ad_id, "variant": v} for ad_id, v in zip(ids["ad_ids"], plan.variants)]
    try:
        banner_problems = await attach_banners(client, biz, created, photos, logo, ai_banners) if (photos or ai_banners) else []
        await client.moderate_ads(ids["ad_ids"])
    except DirectError:
        try:  # откат: не оставляем в кабинете недоделанную кампанию
            await client.delete_campaigns([ids["campaign_id"]])
        except DirectError:
            pass
        raise

    row = conn.execute(
        "INSERT INTO campaigns (business_id, account_id, name, source, external_id, weekly_micro, strategy, state, created_day, last_changed_day) "
        "VALUES (?,?,?, 'direct', ?,?, 'clicks', 'moderation', ?,?)",
        (biz["id"], account_id, f"Реклама: {biz['name']}", ids["campaign_id"], plan.campaign_micro, today, today),
    )
    unit_ids = [
        # бюджет у объявлений общий (недельный лимит кампании), поэтому budget_micro у них 0
        save_unit(conn, biz, account_id, row.lastrowid, c["variant"], {"campaign_id": ids["campaign_id"], "group_id": ids["group_id"], "ad_id": c["ad_id"]},
                  0, today, style=style_for(i),
                  photo_type=ai_scenes.get((c["variant"]["title"], c["variant"]["text"])) or (photo_for(photos, i)[0] if photos else ""),
                  angle=c["variant"].get("angle", ""))
        for i, c in enumerate(created)
    ]
    if banner_problems:
        conn.execute("UPDATE ad_units SET status_note = ? WHERE id = ?", ("Картинки добавлены не ко всем объявлениям", unit_ids[0]))
    conn.execute("UPDATE businesses SET account_id = ?, status = 'running' WHERE id = ?", (account_id, biz["id"]))
    conn.commit()
    return unit_ids


# ---------- синхронизация с кабинетом ----------

def map_state(state: str, status: str, payment: str) -> str:
    """Состояние кампании в Директе → понятное состояние объявления у нас."""
    if status == "DRAFT":
        return "draft"
    if status == "MODERATION":
        return "moderation"
    if status == "REJECTED":
        return "rejected"
    if state == "ON":
        return "active"
    if state == "OFF" and payment == "DISALLOWED":
        return "unpaid"
    return "paused"


def map_ad_state(ad: dict, campaign_state: str) -> str:
    """Состояние объявления у нас. Сначала то, что решили про само объявление (проверка, ручная остановка),
    потом то, что известно про кампанию (счёт не пополнен, кампания остановлена). State объявления в Директе уже учитывает кампанию."""
    status, state = ad.get("Status", ""), ad.get("State", "")
    if status == "REJECTED":
        return "rejected"
    if state == "SUSPENDED":
        return "paused"
    if state == "ON":
        return "active"
    if status == "DRAFT":
        return "draft"
    if status == "MODERATION":
        return "moderation"
    return campaign_state if campaign_state != "active" else "paused"   # выключено сайтом-монитором или похожей причиной


async def sync_units(client: DirectClient, conn: sqlite3.Connection, biz, today: str, days: int = 30) -> dict:
    """Подтягивает из Директа состояния кампании и объявлений и статистику по объявлениям."""
    units = conn.execute(
        "SELECT * FROM ad_units WHERE business_id = ? AND external_campaign_id IS NOT NULL", (biz["id"],)
    ).fetchall()
    if not units:
        return {"units": 0, "rows": 0}
    ids = sorted({u["external_campaign_id"] for u in units})
    result = await client.call("campaigns", "get", {
        "SelectionCriteria": {"Ids": ids},
        "FieldNames": ["Id", "State", "Status", "StatusPayment", "StatusClarification"],
    })
    by_id = {c["Id"]: c for c in result.get("Campaigns", [])}
    ads = {str(a["Id"]): a for a in await client.list_ads(ids)}
    for cid in ids:
        mine = [u for u in units if u["external_campaign_id"] == cid]
        c = by_id.get(cid)
        if c is None:
            for u in mine:
                conn.execute("UPDATE ad_units SET state = 'paused', status_note = 'Кампанию удалили в кабинете Директа' WHERE id = ?", (u["id"],))
            conn.execute("UPDATE campaigns SET state = 'paused' WHERE business_id = ? AND external_id = ?", (biz["id"], cid))
            continue
        cstate = map_state(c.get("State", ""), c.get("Status", ""), c.get("StatusPayment", ""))
        halted = cstate == "active" and biz["status"] == "stopped"
        if halted:
            # стоп-кран нажат: кампанию, которую Яндекс только что одобрил или владелец оплатил, не пускаем в показ
            await client.set_campaigns_state([cid], False)
            cstate = "paused"
        conn.execute("UPDATE campaigns SET state = ? WHERE business_id = ? AND external_id = ?", (cstate, biz["id"], cid))
        for u in mine:
            ad = ads.get(str(u["external_ad_id"]))
            if ad is None:
                state, note = "paused", "Объявление удалили в кабинете Директа"
            else:
                state = "paused" if halted else map_ad_state(ad, cstate)
                note = ad.get("StatusClarification") or c.get("StatusClarification") or ""
            conn.execute("UPDATE ad_units SET state = ?, status_note = ? WHERE id = ?", (state, note, u["id"]))

    goal = biz["metrika_goal_id"] or None
    rows = await client.ad_stats(day_add(today, -(days - 1)), today, goal_id=goal)
    by_ad = {str(u["external_ad_id"]): u["id"] for u in units}
    n = 0
    for r in rows:
        unit_id = by_ad.get(str(r["ad_id"]))
        if unit_id is None:
            continue
        conn.execute(
            "INSERT INTO stats_daily (unit_id, day, impressions, clicks, spend_micro, leads) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(unit_id, day) DO UPDATE SET impressions = excluded.impressions, clicks = excluded.clicks, "
            "spend_micro = excluded.spend_micro, leads = excluded.leads",
            (unit_id, r["day"], r["impressions"], r["clicks"], r["spend_micro"], r["leads"] if goal else 0),
        )
        n += 1
    conn.commit()
    return {"units": len(units), "rows": n}
