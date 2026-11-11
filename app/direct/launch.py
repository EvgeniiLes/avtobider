"""Запуск рекламы бизнеса в Яндекс Директе: план, создание кампаний, отправка на проверку, синхронизация.

Устройство: каждая группа объявлений (до трёх) живёт в своей кампании со своим недельным лимитом: бюджет задаётся на группу, а не на всё сразу.
Внутри кампании одна группа с явными запросами (ключевые фразы из профиля
аудитории и автотаргетинг с выбранными категориями, см. app/direct/audience.py) и 1-3 объявления-варианта. Варианты одной группы показываются в одних и тех же аукционах, так что сравнение честное, а ставки
подбирает сам Яндекс: сначала по кликам, а когда набирается 10 обращений в неделю, по обращениям (см. app/engine/cycle.py).
Раньше на каждый вариант была своя кампания; на малом бюджете у такой кампании не набиралось данных ни для сравнения,
ни для обучения стратегии Яндекса (ему нужно не меньше 10 конверсий в неделю на кампанию).
"""
import dataclasses
import json
import sqlite3
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from app.creative.compose import style_for
from app.creative.angles import districts_of, is_nationwide, needs_districts
from app.direct.client import RUSSIA_REGION_ID, DirectClient, DirectError
from app.direct.autotarget import ensure_autotargeting
from app.direct.traffic import ensure_traffic
from app.direct.bids import ensure_bids
from app.direct.goals import goal_ids_for_report
from app.direct.marking import is_filled
from app.direct import extras as ad_extras
from app.direct import videos
from app.direct.placement import network_wanted
from app.direct.groups import GROUP_LIMIT, RESPONSIVE_PARTS, SEPARATE_LIMIT, build_groups, group_sizes, score  # noqa: F401  (GROUP_LIMIT и group_sizes берут из launch другие модули)
from app.direct.audience import autotargeting_for, effective_negatives, intents_for
from app.direct.texts import check, draft_variants, mentions_district, near_variants
from app.engine.util import EngineError, MICRO, day_add, plural, rub

MAX_VARIANTS = 6   # столько вариантов готовим и показываем в редакторе; в кампанию входят первые MAX_ADS, остальные запасные
MAX_ADS = 3        # объявлений в кампании: на малом бюджете больше не успеют набрать клики для сравнения
DEFAULT_LIVE = 1   # сколько объявлений идёт в показ, пока владелец не выбрал сам; остальные из списка запасные
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
    campaign_micro: int   # недельный лимит одной кампании: он же бюджет одной группы объявлений (в каждой не больше трёх)
    variants: list[dict]
    region_id: int
    city: str
    href: str
    business_id: int | None
    warnings: list[str]
    counter_ids: list[int] | None = None
    own_count: int = 0
    negatives: list[str] | None = None
    keywords: list[str] | None = None       # ключевые фразы группы; пусто: только автотаргетинг
    autotargeting: dict | None = None       # категории и бренды автотаргетинга
    audience: dict | None = None            # профиль аудитории, по которому всё это собрано
    region_ids: list[int] | None = None     # районы, по которым идёт показ (Директ знает их как отдельные регионы); пусто: показ по region_id
    districts: list[str] | None = None      # районы, названные владельцем
    ad_description: str = ""                # «описание объекта продвижения» для маркировки: без него Директ ограничивает показы по закону «О рекламе»
    responsive: bool = False                # комбинаторные объявления: в каждой группе одно объявление с двумя заголовками и двумя текстами
    groups: list[dict] | None = None        # группы-намерения: [{intent, keywords, variants, indexes}], по одной кампании на группу
    separate: bool = False                  # каждое объявление в своей кампании, недельный бюджет запуска делится между ними поровну
    extras: dict | None = None              # второй заголовок, уточнения и отображаемая ссылка (app/direct/extras.py); None: объявления без дополнений
    network: bool = True                    # показывать и в рекламной сети; False: только поиск (режим бизнеса, см. app/direct/placement.py)


def ad_description_of(biz) -> str:
    """Описание объекта продвижения для маркировки: чем занимается рекламодатель, коротко (Директ показывает его в данных об объявлении)."""
    return f"{biz['kind']} «{biz['name']}»".strip()[:120]


UTM = "utm_source=yandex&utm_medium=cpc&utm_campaign={campaign_id}&utm_content={ad_id}&utm_term={keyword}"   # {…} подставляет Директ при каждом клике


def tagged_href(url: str) -> str:
    """Ссылка объявления с метками: в Метрике и в формах сайта видно, с какого объявления и по какой фразе пришёл человек.
    Если у адреса уже есть свои utm-метки или нет схемы, ссылку не трогаем."""
    parts = urlsplit(url or "")
    if not parts.scheme or not parts.netloc or "utm_source=" in parts.query:
        return url
    query = f"{parts.query}&{UTM}" if parts.query else UTM
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", query, parts.fragment))


def _maps_id(biz) -> int | None:
    from app.routes.businesses import maps_org_id  # импорт здесь, чтобы не было круга
    value = maps_org_id(biz["maps_url"])
    return int(value) if value else None


def text_candidates(conn: sqlite3.Connection, biz) -> list[dict]:
    """Тексты для объявлений по порядку: сначала одобренные владельцем, затем предложенные ИИ (по одному на угол подачи), потом шаблоны.
    Отклонённые не берём. Так новые тексты от ИИ попадают в рекламу, не дожидаясь одобрения, а владелец может их убрать."""
    rows = conn.execute(
        "SELECT id, title, body, angle, hook, status, source, position FROM creatives WHERE business_id = ? AND kind = 'text' "
        "AND status IN ('approved', 'draft') ORDER BY id", (biz["id"],)).fetchall()
    chosen = [{"id": r["id"], "title": r["title"], "text": r["body"], "angle": r["angle"], "hook": r["hook"], "status": "approved", "position": r["position"]}
              for r in rows if r["status"] == "approved"]
    # шаблонный текст, сохранённый ради места в списке, угол не занимает: иначе он вытесняет черновики ИИ с тем же углом
    angles = {r["angle"] for r in rows if r["status"] == "approved" and r["angle"] and r["source"] != "template"}
    from app.creative.texts_ai import facts_of  # здесь, чтобы не было круга
    from app.direct.texts import unsupported_claims
    facts = facts_of(biz)
    for r in reversed([r for r in rows if r["status"] == "draft" and r["source"] == "claude"]):  # новые сначала
        if r["angle"] and r["angle"] in angles:
            continue
        if unsupported_claims(f"{r['title']} {r['body']}", facts):   # старый черновик со скидкой, когда скидки выключены, в рекламу не идёт
            continue
        angles.add(r["angle"])
        chosen.append({"id": r["id"], "title": r["title"], "text": r["body"], "angle": r["angle"], "hook": r["hook"], "status": "draft", "position": r["position"]})
    chosen.sort(key=lambda c: c["position"] if c["position"] is not None else 1 << 30)   # порядок, который владелец задал перетаскиванием; остальные следом в прежнем порядке
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
    if start_pool < min_weekly_micro:
        return start_pool, 0
    chosen = biz["ads_live"] if "ads_live" in biz.keys() else None   # владелец сам решил, сколько объявлений пустить в рекламу
    return start_pool, max(1, min(MAX_VARIANTS, chosen)) if chosen else DEFAULT_LIVE   # без выбора в показ идёт одно: весь бюджет на него, остальные запасные для сравнения и замены


def choose_variants(biz, custom: list[dict] | None, count: int) -> tuple[list[dict], int]:
    """Сначала тексты, которые владелец одобрил; недостающие добираем из шаблонов без повторов. Второе число: сколько своих."""
    variants = [{"title": c["title"], "text": c["text"], "angle": c.get("angle", ""), "hook": c.get("hook", "")}
                for c in (custom or []) if not check(c["title"], c["text"])][:count]
    own = len(variants)
    for v in draft_variants(biz, MAX_VARIANTS):
        if len(variants) >= count:
            break
        if v["title"].lower() not in {x["title"].lower() for x in variants}:
            variants.append(v)
    return _with_near_ads(biz, variants, count, {c["title"].lower() for c in custom or [] if c.get("status") == "approved"}), own


def _with_near_ads(biz, variants: list[dict], count: int, approved: set[str]) -> list[dict]:
    """Реклама идёт по районам, поэтому минимум два объявления (одно, если их меньше трёх) прямо говорят, что мы рядом, и называют район.
    Недостающие встают после текстов, которые владелец одобрил подряд с начала списка, и вытесняют только то, что он не одобрял."""
    districts = districts_of(biz)
    if not districts:
        return variants
    near = [v for v in variants if mentions_district(v, districts)]
    need = 2 if count >= 3 else 1
    if len(near) >= need:
        return variants
    free = sum(1 for v in variants if v["title"].lower() not in approved and v not in near) + max(0, count - len(variants))   # места, которые можно отдать
    have = {v["title"].lower() for v in variants}
    extra = [v for v in near_variants(biz) if v["title"].lower() not in have][:min(need - len(near), free)]
    lead = 0
    while lead < len(variants) and variants[lead]["title"].lower() in approved:
        lead += 1
    lead = max(lead, 1) if variants else 0
    merged = variants[:lead] + extra + variants[lead:]
    while len(merged) > count:   # лишнее убираем с конца: сначала то, что не одобрено владельцем и не про район
        drop = next((i for i in range(len(merged) - 1, -1, -1)
                     if merged[i]["title"].lower() not in approved and merged[i] not in near and merged[i] not in extra), len(merged) - 1)
        merged.pop(drop)
    return merged

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


def plan_launch(biz, region_id: int | None, min_weekly_micro: int = MIN_WEEKLY, custom: list[dict] | None = None,
                audience: dict | None = None, district_ids: list[int] | None = None, responsive: bool = False, separate: bool = False) -> Plan:
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
    districts = districts_of(biz)
    if needs_districts(biz) and not districts:
        has_address = bool((biz["address"] or "").strip())
        raise EngineError(("Не удалось определить район по вашему адресу. Впишите район вручную в поле «Районы» на странице «Бизнес»" if has_address else
                           "Укажите адрес (улица и дом) или район на странице «Бизнес»") +
                          ": реклама показывается людям из вашего района, а часть объявлений говорит, что вы рядом с ними.", "no_districts")

    start_pool, count = start_split(biz, min_weekly_micro)
    if count < 1:
        need = rub(min_weekly_micro / START_SHARE / (1 - RESERVE))
        raise EngineError(f"Бюджет слишком мал: на одну кампанию Яндекс требует минимум {rub(min_weekly_micro)} в неделю. Нужно хотя бы {need} в неделю.", "budget_too_small")

    if responsive and not (biz["ads_live"] if "ads_live" in biz.keys() else None):
        count = RESPONSIVE_PARTS   # комбинаторное объявление одно, но держит два заголовка и два текста
    separate = separate and not responsive
    if separate:
        count = max(1, min(count, start_pool // min_weekly_micro))   # у каждой кампании свой лимит, и он не меньше минимума Директа
    variants, own = choose_variants(biz, custom, count)
    warnings = []
    if districts and sum(1 for v in variants[:count] if mentions_district(v, districts)) < (2 if count >= 3 else 1):
        warnings.append("В показ идёт меньше двух объявлений, где назван район и сказано, что вы рядом (нужны как минимум два). "
                        "Это потому, что вы одобрили свои тексты без района: добавьте в какие-нибудь из них район или замените их.")
    for v in variants:
        warnings += check(v["title"], v["text"])
    if custom and own < min(count, 2):
        warnings.append("Одобрено мало своих вариантов. Для сравнения лучше одобрить хотя бы 2–3 на вкладке «Объявления».")
    advised = ads_for_budget(start_pool)
    per_group = max(group_sizes(count))   # бюджет у каждой группы свой, поэтому и достаточность считаем на группу
    if per_group > advised:
        warnings.append(f"Вы выбрали {plural(count, 'объявление', 'объявления', 'объявлений')}: в группе их до {per_group}, а бюджету на группу подходит {advised}. "
                        "На каждое придётся меньше кликов, и сравнить варианты будет труднее.")
    from app.direct.advice import budget_advice   # здесь, чтобы не было круга
    advice = budget_advice(biz, biz["description"])
    if advice["level"] == "low":   # хватает на запуск и сравнение, но не на подбор ставок под обращения: владелец должен знать об этом до запуска
        warnings.append(advice["summary"])
    counters = [int(biz["metrika_counter_id"])] if biz["metrika_counter_id"] else None
    keywords = list((audience or {}).get("keywords") or [])
    keywords += _district_phrases(keywords, districts)
    by_district = bool(districts) and len(district_ids or []) == len(districts)   # показ по районам только если Яндекс знает их все: иначе часть районов потерялась бы молча
    if districts and not by_district:
        listed = ", ".join(districts)
        warnings.append(f"Яндекс знает как отдельную область показа не все районы из списка ({listed}). Реклама пойдёт по всему городу, "
                        "а названия районов добавлены в тексты и ключевые фразы. Если район написан иначе, чем у Яндекса, поправьте его на странице «Бизнес».")
    if audience is None:
        warnings.append("Профиль аудитории не составлен, ключевых фраз нет: показ пойдёт только по автотаргетингу Яндекса. "
                        "Составьте профиль на шаге «Запуск», чтобы запросы подбирались под вашего клиента.")
    elif len(keywords) < 5:
        warnings.append("В профиле мало ключевых фраз: большую часть запросов подберёт автотаргетинг Яндекса. Добавьте свои фразы в профиле аудитории.")
    intents = intents_for(audience)
    groups = build_groups(variants[:count], intents or [{"name": "", "keywords": [k for k in keywords if k not in set(_district_phrases(keywords, districts))]}],
                          RESPONSIVE_PARTS if responsive else SEPARATE_LIMIT if separate else GROUP_LIMIT)
    for g in groups:   # районы добавляются к фразам каждой группы: «стрижка собак хамовники»
        g["keywords"] = g["keywords"] + _district_phrases(g["keywords"], districts)
    per_campaign = start_pool // max(1, len(groups)) if separate else start_pool   # объявление в своей кампании: общий бюджет запуска делится поровну
    return Plan(
        responsive=responsive, separate=separate, groups=groups, count=count, campaign_micro=per_campaign, variants=variants, region_id=RUSSIA_REGION_ID if nationwide else region_id, ad_description=ad_description_of(biz),
        city="вся Россия" if nationwide else biz["city"], href=biz["site_url"], business_id=_maps_id(biz), warnings=warnings, counter_ids=counters,
        own_count=own, negatives=effective_negatives(biz, audience), keywords=keywords, autotargeting=autotargeting_for(audience), audience=audience,
        region_ids=list(district_ids) if by_district and not nationwide else None, districts=districts or None, network=network_wanted(biz),
    )


def _district_phrases(keywords: list[str], districts: list[str], limit: int = 12) -> list[str]:
    """Ключевые фразы с районом: «маникюр» + «Хамовники» → «маникюр хамовники». Так реклама ловит запросы людей, которые ищут рядом с собой."""
    have = {k.lower() for k in keywords}
    out = []
    for base in keywords[:4]:
        for d in districts:
            phrase = f"{base} {d}".lower()
            if d.lower() not in base.lower() and phrase not in have and len(phrase) <= 80 and phrase not in out:   # район в фразе уже есть: второй раз не дописываем
                out.append(phrase)
    return out[:limit]


def campaign_name(biz, number: int, intent: str = "") -> str:
    """Каждая группа объявлений живёт в своей кампании со своим бюджетом. Если группа это услуга (намерение), её название в имени кампании;
    иначе у первой кампании имя бизнеса, у следующих номер."""
    base = f"Автобидер: {biz['name']}"
    if intent:
        return f"{base} — {intent}"[:255]
    return base if number == 0 else f"{base} {number + 1}"


async def extension_ids(client: DirectClient, plan: Plan) -> list[int]:
    """Уточнения плана в кабинет (один раз на запуск, одни и те же у всех объявлений). Сбой не ломает запуск: уточнения не обязательны."""
    try:
        return await client.add_callouts((plan.extras or {}).get("callouts") or [])
    except DirectError:
        return []


async def create_campaign(client: DirectClient, plan: Plan, name: str, start_date: str, variants: list[dict], ext_ids: list[int] | None = None,
                          keywords: list[str] | None = None, intent: str = "", responsive: bool = False) -> dict:
    """Создаёт кампанию со своим недельным бюджетом, одну группу и в ней объявления (не больше трёх). Если что-то не вышло, удаляет созданное и пробрасывает ошибку."""
    campaign_id = None
    try:
        campaign_id = await client.create_text_campaign(
            name, plan.campaign_micro, start_date, counter_ids=plan.counter_ids, negative_keywords=plan.negatives, network=plan.network)
        group_id = await client.create_ad_group(campaign_id, (intent or "Основная")[:255], plan.region_ids or [plan.region_id])
        added = await client.add_keywords(group_id, plan.keywords or [] if keywords is None else keywords, plan.autotargeting)
        ad_ids, kind, fallback = [], "text", ""
        titles = list(dict.fromkeys(v["title"] for v in variants))[:RESPONSIVE_PARTS]
        texts = list(dict.fromkeys(v["text"] for v in variants))[:RESPONSIVE_PARTS]
        if responsive:
            try:   # одно комбинаторное объявление с несколькими заголовками и текстами; не приняли: делаем обычные объявления, запуск не ломаем
                ad_ids = [await client.create_responsive_ad(group_id, titles, texts, href=tagged_href(plan.href), business_id=plan.business_id,
                                                            erir_description=plan.ad_description, display_path=ad_extras.fit_path(plan.extras),
                                                            extension_ids=ext_ids)]
                kind = "responsive"
            except DirectError as e:
                fallback = e.full_message()
        if kind == "text":
            for v in variants:
                ad_ids.append(await client.create_text_ad(group_id, v["title"], v["text"], href=tagged_href(plan.href), business_id=plan.business_id,
                                                          erir_description=plan.ad_description, title2=ad_extras.title2_for(plan.extras, v["title"]),
                                                          display_path=ad_extras.fit_path(plan.extras), extension_ids=ext_ids))
    except DirectError:
        if campaign_id:
            try:
                await client.delete_campaigns([campaign_id])
            except DirectError:
                pass
        raise
    return {"campaign_id": campaign_id, "group_id": group_id, "ad_ids": ad_ids, "keywords": added, "kind": kind, "fallback": fallback,
            "titles": titles, "texts": texts}


def created_ads(ids: dict, variants: list[dict], indexes: list[int]) -> list[dict]:
    """Объявления, которые создала кампания: у комбинаторной группы одно объявление на все её варианты, у обычной по объявлению на вариант."""
    if ids["kind"] == "responsive":
        return [{"ad_id": ids["ad_ids"][0], "variant": variants[0], "index": indexes[0], "responsive": True, "variants": variants, "indexes": indexes,
                 "titles": ids["titles"], "texts": ids["texts"]}]
    return [{"ad_id": ad_id, "variant": v, "index": index} for ad_id, v, index in zip(ids["ad_ids"], variants, indexes)]


def save_unit(conn: sqlite3.Connection, biz, account_id: int, campaign_row_id: int, variant: dict, ids: dict,
              weekly_micro: int, today: str, variant_of: int | None = None, style: str = "", photo_type: str = "",
              angle: str = "", ad_kind: str = "", variants_json: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO ad_units (campaign_id, business_id, title, body, state, budget_micro, variant_of, last_changed_day, created_day, "
        "external_campaign_id, external_group_id, external_ad_id, style, photo_type, angle, ad_kind, variants_json) VALUES (?,?,?,?, 'moderation', ?,?,?,?,?,?,?,?,?,?,?,?)",
        (campaign_row_id, biz["id"], variant["title"], variant["text"], weekly_micro, variant_of, today, today,
         ids["campaign_id"], ids["group_id"], ids["ad_id"], style, photo_type, angle, ad_kind, variants_json),
    )
    from app.direct import moderation   # здесь, чтобы не было круга
    moderation.mark_pending(conn, biz["id"], cur.lastrowid)   # ушло на проверку: когда Яндекс его примет, владельцу придёт сообщение
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
        "WHERE c.business_id = ? AND c.kind = 'image' AND c.status IN ('approved', 'draft') ORDER BY c.id", (biz["id"],)
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
        "WHERE c.business_id = ? AND c.kind = 'image' AND c.status IN ('approved', 'draft') ORDER BY c.id", (biz["id"],)
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
        if c.get("responsive"):   # у комбинаторного объявления до пяти картинок: баннеры всех его вариантов
            hashes = []
            for variant, place in zip(c["variants"], c["indexes"]):
                try:
                    banner = (ai or {}).get((variant["title"], variant["text"]))   # только баннер, нарисованный нейросетью: шаблонные в рекламу не идут
                    if banner is not None:
                        hashes.append(await client.upload_image(banner, f"autobider-{biz['id']}-{place + 1}-{c['ad_id']}.jpg"))
                except DirectError as e:
                    problems.append(f"«{variant['title']}»: {e.message}")
                except Exception:
                    problems.append(f"«{variant['title']}»: не удалось подготовить картинку")
            if hashes:
                try:
                    await client.attach_responsive_images(c["ad_id"], hashes)
                    c["image_hash"] = hashes[0]
                except DirectError as e:
                    problems.append(f"«{c['variant']['title']}»: {e.message}")
            continue
        try:
            place = c.get("index", i)   # номер в списке объявлений: от него зависят шаблон и фото баннера
            ready = (ai or {}).get((c["variant"]["title"], c["variant"]["text"]))
            if ready is None:
                continue   # баннер ещё не нарисован: объявление идёт без картинки, шаблонную заглушку не ставим
            banner = ready  # ИИ-баннер целиком
            image_hash = await client.upload_image(banner, f"autobider-{biz['id']}-{place + 1}-{c['ad_id']}.jpg")
            await client.attach_image(c["ad_id"], image_hash)
            c["image_hash"] = image_hash
        except DirectError as e:
            problems.append(f"«{c['variant']['title']}»: {e.message}")
        except Exception:  # битая картинка не должна ломать запуск
            problems.append(f"«{c['variant']['title']}»: не удалось подготовить картинку")
    return problems


async def launch(client: DirectClient, conn: sqlite3.Connection, biz, account_id: int, plan: Plan, today: str) -> list[int]:
    """Создаёт в кабинете кампании по группам-намерениям с объявлениями, добавляет баннеры, отправляет на проверку и записывает в базу."""
    start_date = day_add(today, 1)
    photos, logo = media_typed(conn, biz)
    ai_banners = ai_banners_for(conn, biz)
    ai_scenes = ai_scenes_for(conn, biz)
    ext_ids = await extension_ids(client, plan)
    groups = plan.groups or build_groups(plan.variants, [{"name": "", "keywords": plan.keywords or []}])
    made = []   # (кампания в Директе, её объявления, намерение): каждая группа объявлений идёт в своей кампании со своим бюджетом
    banner_problems = []
    try:
        for number, g in enumerate(groups):
            ids = await create_campaign(client, plan, campaign_name(biz, number, g["intent"]), start_date, g["variants"], ext_ids, g["keywords"], g["intent"],
                                        plan.responsive)
            created = created_ads(ids, g["variants"], g["indexes"])
            made.append((ids, created, g["intent"]))
            if photos or ai_banners:
                banner_problems += await attach_banners(client, biz, created, photos, logo, ai_banners)
        await client.moderate_ads([ad_id for ids, _, _ in made for ad_id in ids["ad_ids"]])
    except DirectError:
        try:  # откат: не оставляем в кабинете недоделанные кампании
            if made:
                await client.delete_campaigns([ids["campaign_id"] for ids, _, _ in made])
        except DirectError:
            pass
        raise

    unit_ids = []
    for number, (ids, created, intent) in enumerate(made):
        row = conn.execute(
            "INSERT INTO campaigns (business_id, account_id, name, source, external_id, weekly_micro, strategy, state, created_day, last_changed_day, intent) "
            "VALUES (?,?,?, 'direct', ?,?, 'clicks', 'moderation', ?,?,?)",
            (biz["id"], account_id, campaign_name(biz, number, intent).replace("Автобидер", "Реклама", 1), ids["campaign_id"], plan.campaign_micro, today, today, intent),
        )
        unit_ids += [
            # бюджет у объявлений общий (недельный лимит их кампании), поэтому budget_micro у них 0
            save_unit(conn, biz, account_id, row.lastrowid, c["variant"], {"campaign_id": ids["campaign_id"], "group_id": ids["group_id"], "ad_id": c["ad_id"]},
                      0, today, style=style_for(c["index"]),
                      photo_type=ai_scenes.get((c["variant"]["title"], c["variant"]["text"])) or (photo_for(photos, c["index"])[0] if photos else ""),
                      angle=c["variant"].get("angle", ""), ad_kind="responsive" if c.get("responsive") else "",
                      variants_json=json.dumps({"titles": c["titles"], "texts": c["texts"]}, ensure_ascii=False) if c.get("responsive") else "")
            for c in created
        ]
        if ids["fallback"]:
            conn.execute("UPDATE ad_units SET status_note = ? WHERE id = ?",
                         (f"Комбинаторное объявление Директ не принял, сделаны обычные: {ids['fallback']}", unit_ids[-len(created)]))
    if banner_problems:
        conn.execute("UPDATE ad_units SET status_note = ? WHERE id = ?", ("Картинки добавлены не ко всем объявлениям", unit_ids[0]))
    conn.execute("UPDATE businesses SET account_id = ?, status = 'running' WHERE id = ?", (account_id, biz["id"]))
    conn.commit()
    _remember_keywords(conn, biz, made[0][0]["keywords"])
    return unit_ids


async def split_campaign(client: DirectClient, conn: sqlite3.Connection, biz, campaign_row_id: int, plan: Plan, today: str) -> list[int]:
    """Сравнение на уже идущей рекламе: каждое работающее объявление общей кампании получает свою новую кампанию (с теми же фразами, баннером и
    районами), недельный лимит старой делится между ними поровну, а старые объявления и кампания останавливаются. Через несколько дней сервис сам
    сравнит новые объявления (они запущены в один день) и оставит лучшее."""
    old = conn.execute("SELECT * FROM campaigns WHERE id = ? AND business_id = ? AND source = 'direct' AND external_id IS NOT NULL", (campaign_row_id, biz["id"])).fetchone()
    units = conn.execute("SELECT * FROM ad_units WHERE campaign_id = ? AND state = 'active' AND ad_kind != 'responsive' ORDER BY id", (campaign_row_id,)).fetchall() if old else []
    if len(units) < 2:
        raise EngineError("В кампании меньше двух работающих обычных объявлений: разносить нечего.", "nothing_to_split")
    if old["weekly_micro"] // len(units) < MIN_WEEKLY:
        raise EngineError(f"Лимит кампании {rub(old['weekly_micro'])} в неделю не делится на {len(units)}: на каждую кампанию Яндекс требует не меньше {rub(MIN_WEEKLY)}.", "budget_too_small")
    share = dataclasses.replace(plan, campaign_micro=old["weekly_micro"] // len(units), separate=True)
    start_date = day_add(today, 1)
    photos, logo = media_typed(conn, biz)
    ai_banners = ai_banners_for(conn, biz)
    ext_ids = await extension_ids(client, share)
    keywords = next((g["keywords"] for g in plan.groups or [] if g["intent"] == old["intent"]), None) or share.keywords   # фразы услуги этой кампании из профиля, с районами
    made, problems = [], []
    have = conn.execute("SELECT COUNT(*) FROM campaigns WHERE business_id = ? AND source = 'direct'", (biz["id"],)).fetchone()[0]
    try:
        for n, u in enumerate(units):
            variant = {"title": u["title"], "text": u["body"]}
            ids = await create_campaign(client, share, campaign_name(biz, have + n, old["intent"]), start_date, [variant], ext_ids, keywords, old["intent"], False)
            created = created_ads(ids, [variant], [u["id"]])
            made.append((ids, created, u))
            if photos or ai_banners:
                problems += await attach_banners(client, biz, created, photos, logo, ai_banners)
        await client.moderate_ads([ad_id for ids, _, _ in made for ad_id in ids["ad_ids"]])
    except DirectError:
        try:   # откат: старая кампания не тронута, недоделанные новые убираем
            if made:
                await client.delete_campaigns([ids["campaign_id"] for ids, _, _ in made])
        except DirectError:
            pass
        raise
    await client.set_ads_state([int(u["external_ad_id"]) for u in units], False)   # новые ушли на проверку: старые останавливаем, чтобы не тратить бюджет дважды
    await client.set_campaigns_state([int(old["external_id"])], False)
    unit_ids = []
    for n, (ids, created, u) in enumerate(made):
        row = conn.execute(
            "INSERT INTO campaigns (business_id, account_id, name, source, external_id, weekly_micro, strategy, state, created_day, last_changed_day, intent) "
            "VALUES (?,?,?, 'direct', ?,?, 'clicks', 'moderation', ?,?,?)",
            (biz["id"], old["account_id"], campaign_name(biz, have + n, old["intent"]).replace("Автобидер", "Реклама", 1), ids["campaign_id"], share.campaign_micro, today, today, old["intent"]))
        unit_ids += [save_unit(conn, biz, old["account_id"], row.lastrowid, c["variant"], {"campaign_id": ids["campaign_id"], "group_id": ids["group_id"], "ad_id": c["ad_id"]},
                               0, today, style=u["style"], photo_type=u["photo_type"], angle=u["angle"]) for c in created]
    conn.execute(f"UPDATE ad_units SET state = 'paused', last_changed_day = ? WHERE id IN ({','.join('?' * len(units))})", (today, *[u["id"] for u in units]))
    conn.execute("UPDATE campaigns SET state = 'paused', last_changed_day = ? WHERE id = ?", (today, campaign_row_id))
    if problems:
        conn.execute("UPDATE ad_units SET status_note = ? WHERE id = ?", ("Картинки добавлены не ко всем объявлениям", unit_ids[0]))
    conn.commit()
    return unit_ids


async def launch_ab(client: DirectClient, conn: sqlite3.Connection, biz, original_id: int, variant: dict, index: int, plan: Plan, today: str) -> int:
    """A/B на идущей рекламе: ОДНО новое объявление в своей кампании против идущего. Бюджет новой кампании равен доле идущего объявления в его кампании
    (лимит кампании, поделённый на число её работающих объявлений), чтобы сравнение было честным. Запись в experiments: дальше сравнение ведёт движок
    (через неделю или набрав кликов оставляет сильного и останавливает проигравшего). Нужен нарисованный баннер: без него сравнивать нечестно."""
    orig = conn.execute(
        "SELECT u.*, c.weekly_micro AS camp_weekly, c.account_id AS camp_account, c.intent AS camp_intent FROM ad_units u JOIN campaigns c ON c.id = u.campaign_id "
        "WHERE u.id = ? AND u.business_id = ? AND c.source = 'direct'", (original_id, biz["id"])).fetchone()
    if orig is None or orig["state"] != "active" or orig["ad_kind"] == "responsive":
        raise EngineError("Сравнивать можно только с работающим обычным объявлением.", "nothing_to_compare")
    if conn.execute("SELECT 1 FROM ad_units WHERE business_id = ? AND lower(title) = lower(?)", (biz["id"], variant["title"])).fetchone():
        raise EngineError("Такое объявление уже есть в рекламе. Измените заголовок нового варианта.", "duplicate")
    if conn.execute("SELECT 1 FROM experiments WHERE business_id = ? AND status = 'running' AND (unit_a = ? OR unit_b = ?)", (biz["id"], original_id, original_id)).fetchone():
        raise EngineError("Это объявление уже участвует в сравнении. Дождитесь его конца.", "already")
    ai_banners = ai_banners_for(conn, biz)
    if (variant["title"], variant["text"]) not in ai_banners:
        raise EngineError("Сначала нарисуйте баннер для нового варианта: без него сравнение с идущим объявлением нечестное.", "no_banner")
    working = conn.execute("SELECT COUNT(*) FROM ad_units WHERE campaign_id = ? AND state = 'active'", (orig["campaign_id"],)).fetchone()[0]
    budget = orig["camp_weekly"] // max(1, working)
    if budget < MIN_WEEKLY:
        raise EngineError(f"Доля идущего объявления {rub(budget)} в неделю меньше минимума Директа {rub(MIN_WEEKLY)}: на новую кампанию денег не хватит.", "budget_too_small")
    share = dataclasses.replace(plan, campaign_micro=budget, separate=True)
    photos, logo = media_typed(conn, biz)
    ext_ids = await extension_ids(client, share)
    keywords = next((g["keywords"] for g in plan.groups or [] if g["intent"] == orig["camp_intent"]), None) or share.keywords
    have = conn.execute("SELECT COUNT(*) FROM campaigns WHERE business_id = ? AND source = 'direct'", (biz["id"],)).fetchone()[0]
    name = campaign_name(biz, have, orig["camp_intent"])
    ids = await create_campaign(client, share, name, day_add(today, 1), [variant], ext_ids, keywords, orig["camp_intent"], False)
    try:
        created = created_ads(ids, [variant], [index])
        problems = await attach_banners(client, biz, created, photos, logo, ai_banners)
        await client.moderate_ads(ids["ad_ids"])
    except DirectError:
        try:
            await client.delete_campaigns([ids["campaign_id"]])
        except DirectError:
            pass
        raise
    row = conn.execute(
        "INSERT INTO campaigns (business_id, account_id, name, source, external_id, weekly_micro, strategy, state, created_day, last_changed_day, intent) "
        "VALUES (?,?,?, 'direct', ?,?, 'clicks', 'moderation', ?,?,?)",
        (biz["id"], orig["camp_account"], name.replace("Автобидер", "Реклама", 1), ids["campaign_id"], budget, today, today, orig["camp_intent"]))
    unit_id = save_unit(conn, biz, orig["camp_account"], row.lastrowid, variant, {"campaign_id": ids["campaign_id"], "group_id": ids["group_id"], "ad_id": created[0]["ad_id"]},
                        0, today, variant_of=original_id, style=orig["style"], photo_type=orig["photo_type"], angle=variant.get("angle", ""))
    conn.execute("INSERT INTO experiments (business_id, unit_a, unit_b, started_day) VALUES (?,?,?,?)", (biz["id"], original_id, unit_id, today))
    if problems:
        conn.execute("UPDATE ad_units SET status_note = ? WHERE id = ?", ("Картинка добавлена не ко всем объявлениям", unit_id))
    conn.commit()
    return unit_id


def existing_campaign(conn: sqlite3.Connection, business_id: int) -> sqlite3.Row | None:
    """Кампания бизнеса в Директе вместе с её группой: куда добавлять новые объявления. None, если рекламы ещё нет."""
    return conn.execute(
        "SELECT c.id AS row_id, c.account_id, u.external_campaign_id, u.external_group_id FROM campaigns c "
        "JOIN ad_units u ON u.campaign_id = c.id WHERE c.business_id = ? AND c.source = 'direct' AND u.external_group_id IS NOT NULL "
        "ORDER BY c.id DESC, u.id DESC LIMIT 1", (business_id,)).fetchone()   # группа самого нового объявления: в неё добавляем, пока есть место


def _existing_groups(conn: sqlite3.Connection, business_id: int, intents: list[dict]) -> list[dict]:
    """Группы бизнеса в Директе: чья она кампания, какое намерение и сколько в ней объявлений."""
    by_name = {i["name"]: i for i in intents}
    rows = conn.execute(
        "SELECT c.id AS row_id, c.account_id, c.intent, u.external_campaign_id AS cid, u.external_group_id AS gid, COUNT(u.id) AS n, MAX(u.ad_kind) AS ad_kind FROM campaigns c "
        "JOIN ad_units u ON u.campaign_id = c.id WHERE c.business_id = ? AND c.source = 'direct' AND u.external_group_id IS NOT NULL "
        "GROUP BY u.external_group_id ORDER BY c.id, u.external_group_id", (business_id,)).fetchall()
    return [{**dict(r), "intent_obj": by_name.get(r["intent"])} for r in rows]


def plan_additions(conn: sqlite3.Connection, biz, plan: Plan, fresh: list[tuple[int, dict]]) -> tuple[list[tuple[int, dict, dict]], list[dict]]:
    """Куда пойдут новые объявления: в существующую группу своего намерения (пока в ней меньше трёх) или в новую кампанию.
    Группа без намерения (запуск до этой схемы) подходит любому объявлению. Возвращает (в существующие группы, новые группы)."""
    intents = intents_for(plan.audience)
    groups = _existing_groups(conn, biz["id"], intents)
    limit = SEPARATE_LIMIT if plan.separate else GROUP_LIMIT
    into, pending = [], {}
    for index, v in fresh:
        scores = [(score(v, i), i) for i in intents]
        top = max(scores, key=lambda x: x[0], default=(0, None))
        target = top[1] if top[0] > 0 and len(intents) > 1 else None
        room = [g for g in groups if g["n"] < limit and g["ad_kind"] != "responsive" and (target is None or g["intent"] in ("", target["name"]))]   # в комбинаторное объявление новые варианты не добавляем
        if room:
            best = room[-1]   # самая новая подходящая группа
            best["n"] += 1
            into.append((index, v, best))
        else:
            key = target["name"] if target else ""
            pending.setdefault(key, {"intent": key, "keywords": (target["keywords"] if target else [k for i in intents for k in i["keywords"]]), "items": []})["items"].append((index, v))
    new_groups = []
    for g in pending.values():
        position = 0
        for size in group_sizes(len(g["items"]), RESPONSIVE_PARTS if plan.responsive else limit):
            new_groups.append({"intent": g["intent"], "keywords": g["keywords"], "items": g["items"][position:position + size]})
            position += size
    return into, new_groups


def new_campaigns_for(conn: sqlite3.Connection, biz, plan: Plan, fresh: list[tuple[int, dict]]) -> int:
    """Сколько новых кампаний понадобится, чтобы добавить эти объявления."""
    return len(plan_additions(conn, biz, plan, fresh)[1])


def new_variants(conn: sqlite3.Connection, biz, plan: Plan) -> list[tuple[int, dict]]:
    """Объявления из списка «пойдут в рекламу», которых в кампании ещё нет (по заголовку), вместе с их номером в списке."""
    have = {r["title"].lower() for r in conn.execute("SELECT title FROM ad_units WHERE business_id = ?", (biz["id"],)).fetchall()}
    return [(i, v) for i, v in enumerate(plan.variants[:plan.count]) if v["title"].lower() not in have]


async def add_ads(client: DirectClient, conn: sqlite3.Connection, biz, target: sqlite3.Row, plan: Plan, today: str) -> list[int]:
    """Добавляет новые объявления из списка: каждое идёт в группу своего намерения (пока в ней меньше трёх объявлений), остальные в новую кампанию
    со своим бюджетом и фразами этого намерения. Тексты, баннеры, отправка на проверку."""
    fresh = new_variants(conn, biz, plan)
    if not fresh:
        raise EngineError("Все объявления из списка уже есть в рекламе. Добавьте на вкладке «Объявления» новый вариант или увеличьте число объявлений в показе.", "nothing_new")
    photos, logo = media_typed(conn, biz)
    ai_banners = ai_banners_for(conn, biz)
    ai_scenes = ai_scenes_for(conn, biz)
    have = conn.execute("SELECT COUNT(*) FROM campaigns WHERE business_id = ? AND source = 'direct'", (biz["id"],)).fetchone()[0]
    into, new_groups = plan_additions(conn, biz, plan, fresh)
    ext_ids = await extension_ids(client, plan)
    created, new_campaigns = [], []   # объявления в старых группах; кампании, созданные для остальных
    try:
        for index, v, g in into:
            ad_id = await client.create_text_ad(g["gid"], v["title"], v["text"], href=tagged_href(plan.href),
                                                business_id=plan.business_id, erir_description=plan.ad_description,
                                                title2=ad_extras.title2_for(plan.extras, v["title"]), display_path=ad_extras.fit_path(plan.extras), extension_ids=ext_ids)
            created.append({"ad_id": ad_id, "variant": v, "index": index, "campaign": None, "row_id": g["row_id"], "ids": {"campaign_id": g["cid"], "group_id": g["gid"]}})
        for g in new_groups:
            ids = await create_campaign(client, plan, campaign_name(biz, have + len(new_campaigns), g["intent"]), day_add(today, 1),
                                        [v for _, v in g["items"]], ext_ids, g["keywords"] or None, g["intent"], plan.responsive)
            new_campaigns.append((ids, g["intent"]))
            created += [{**c, "campaign": len(new_campaigns) - 1, "ids": ids}
                        for c in created_ads(ids, [v for _, v in g["items"]], [index for index, _ in g["items"]])]
        banner_problems = await attach_banners(client, biz, created, photos, logo, ai_banners) if (photos or ai_banners) else []
        await client.moderate_ads([c["ad_id"] for c in created])
    except DirectError:
        try:   # откат: не оставляем в кабинете объявления и кампании, которые не дошли до проверки
            old = [c["ad_id"] for c in created if c["campaign"] is None]
            if old:
                await client.delete_ads(old)
            if new_campaigns:
                await client.delete_campaigns([ids["campaign_id"] for ids, _ in new_campaigns])
        except DirectError:
            pass
        raise
    rows = []
    for n, (ids, intent) in enumerate(new_campaigns):
        rows.append(conn.execute(
            "INSERT INTO campaigns (business_id, account_id, name, source, external_id, weekly_micro, strategy, state, created_day, last_changed_day, intent) "
            "VALUES (?,?,?, 'direct', ?,?, 'clicks', 'moderation', ?,?,?)",
            (biz["id"], target["account_id"], campaign_name(biz, have + n, intent).replace("Автобидер", "Реклама", 1), ids["campaign_id"], plan.campaign_micro, today, today, intent)).lastrowid)
    unit_ids = [
        save_unit(conn, biz, target["account_id"], c["row_id"] if c["campaign"] is None else rows[c["campaign"]], c["variant"],
                  {"campaign_id": c["ids"]["campaign_id"], "group_id": c["ids"]["group_id"], "ad_id": c["ad_id"]}, 0, today,
                  style=style_for(c["index"]),
                  photo_type=ai_scenes.get((c["variant"]["title"], c["variant"]["text"])) or (photo_for(photos, c["index"])[0] if photos else ""),
                  angle=c["variant"].get("angle", ""), ad_kind="responsive" if c.get("responsive") else "",
                  variants_json=json.dumps({"titles": c["titles"], "texts": c["texts"]}, ensure_ascii=False) if c.get("responsive") else "")
        for c in created
    ]
    if banner_problems:
        conn.execute("UPDATE ad_units SET status_note = ? WHERE id = ?", ("Картинка добавлена не ко всем объявлениям", unit_ids[0]))
    conn.commit()
    return unit_ids


def _remember_keywords(conn: sqlite3.Connection, biz, keywords: dict) -> None:
    """Что из запросов реально принял Директ: сколько фраз, какие он отклонил и включился ли автотаргетинг. Это видно владельцу в профиле аудитории."""
    from app.direct import audience  # здесь, чтобы не было круга
    profile = audience.load(biz)
    if profile is None:
        return
    profile["launched"] = {"added": keywords["added"], "rejected": keywords["rejected"],
                           "autotargeting": keywords["autotargeting"], "autotargeting_error": keywords["autotargeting_error"]}
    audience.save(conn, biz["id"], profile)


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


FUNDS_NOTE = ("Объявления одобрены, но показ выключен: скорее всего, на счёте в Директе нет денег. Пополните счёт в кабинете "
              "(direct.yandex.ru, кнопка «Пополнить» слева): сервис сам не принимает платежи.")


async def sync_units(client: DirectClient, conn: sqlite3.Connection, biz, today: str, days: int = 30) -> dict:
    """Подтягивает из Директа состояния кампании и объявлений и статистику по объявлениям."""
    units = conn.execute(
        "SELECT * FROM ad_units WHERE business_id = ? AND external_campaign_id IS NOT NULL", (biz["id"],)
    ).fetchall()
    if not units:
        return {"units": 0, "rows": 0}
    try:
        await videos.advance(client, conn, biz["id"])   # видео, которые Директ уже обработал, встают к своим объявлениям
    except DirectError:
        pass   # не вышло сейчас, повторим при следующей синхронизации: на статистику это не влияет
    ids = sorted({u["external_campaign_id"] for u in units})
    result = await client.call("campaigns", "get", {
        "SelectionCriteria": {"Ids": ids},
        "FieldNames": ["Id", "State", "Status", "StatusPayment", "StatusClarification"],
    })
    by_id = {c["Id"]: c for c in result.get("Campaigns", [])}
    ads = {str(a["Id"]): a for a in await client.list_ads(ids)}
    # без «описания объекта продвижения» Директ ограничивает показы по закону «О рекламе»: дописываем его объявлениям, где его нет, а ответ Директа показываем в сервисе
    missing = [a["Id"] for a in ads.values() if a.get("TextAd") is not None and not (a.get("TextAd") or {}).get("ErirAdDescription")]   # у комбинаторного объявления описание ставится при создании
    marking_note = ""
    if missing:
        try:
            await client.set_ad_descriptions(missing, ad_description_of(biz))
            marking_note = "Добавили описание рекламы для маркировки: без него Директ ограничивал показы по закону «О рекламе». Статус в Директе обновится в течение часа."
        except DirectError as e:
            marking_note = f"Не удалось добавить описание рекламы для маркировки (Директ ограничивает показы по закону «О рекламе»): {e.full_message()}"
    # Директ не отдаёт через API причину «Показы ограничены из-за отсутствия данных для закона «О рекламе»»: определяем её сами и держим пометку, пока данные не внесены
    try:
        advertiser_filled = is_filled(await client.advertiser_data())
    except DirectError:
        advertiser_filled = True   # не удалось прочитать данные: ложную тревогу не поднимаем
    advertiser_note = "" if advertiser_filled else (
        "Директ ограничивает показы по закону «О рекламе»: в кабинете не внесены данные рекламодателя (вид плательщика, ИНН, имя). "
        "Внесите их на странице «Бизнес», в карточке «Данные рекламодателя».")
    for cid in ids:
        mine = [u for u in units if u["external_campaign_id"] == cid]
        c = by_id.get(cid)
        if c is None:
            for u in mine:
                conn.execute("UPDATE ad_units SET state = 'paused', status_note = 'Кампанию удалили в кабинете Директа' WHERE id = ?", (u["id"],))
            conn.execute("UPDATE campaigns SET state = 'paused' WHERE business_id = ? AND external_id = ?", (biz["id"], cid))
            continue
        cstate = map_state(c.get("State", ""), c.get("Status", ""), c.get("StatusPayment", ""))
        started = conn.execute("SELECT created_day FROM campaigns WHERE business_id = ? AND external_id = ?", (biz["id"], cid)).fetchone()
        # Директ не отдаёт через API баланс и не пишет «Средства на счёте закончились». Но кампания, которую одобрили, у которой наступила дата старта,
        # а владелец её не останавливал (тогда было бы SUSPENDED), выключена почти всегда из-за пустого счёта
        no_funds = (cstate == "paused" and c.get("State") == "OFF" and c.get("Status") == "ACCEPTED" and biz["status"] != "stopped"
                    and started is not None and started["created_day"] < today)
        if no_funds:
            cstate = "unpaid"
        halted = cstate == "active" and biz["status"] == "stopped"
        if halted:
            # стоп-кран нажат: кампанию, которую Яндекс только что одобрил или владелец оплатил, не пускаем в показ
            await client.set_campaigns_state([cid], False)
            cstate = "paused"
        conn.execute("UPDATE campaigns SET state = ? WHERE business_id = ? AND external_id = ?", (cstate, biz["id"], cid))
        alert_errors = await client.ensure_alerts(cid)   # мониторинг сайта и почтовые предупреждения включает сам сервис
        network_error = await client.set_network(cid, network_wanted(biz))   # сети включены или выключены так, как выбрано для бизнеса (по умолчанию онлайн-услуге только поиск)
        alert_errors = [*alert_errors, *([network_error] if network_error else [])]
        alerts_note = f"Не удалось включить настройки кампании. {' '.join(alert_errors)}" if alert_errors else ""
        for u in mine:
            ad = ads.get(str(u["external_ad_id"]))
            if ad is None:
                state, note = "paused", "Объявление удалили в кабинете Директа"
            else:
                state = "paused" if halted else map_ad_state(ad, cstate)
                note = ad.get("StatusClarification") or c.get("StatusClarification") or ""
                if state == "unpaid" and c.get("StatusPayment") != "DISALLOWED":
                    note = f"{FUNDS_NOTE} {note}".strip()
                if marking_note and ad["Id"] in missing:
                    note = f"{marking_note} {note}".strip()
                elif advertiser_note:
                    note = f"{advertiser_note} {note}".strip()
                if alerts_note:
                    note = f"{alerts_note} {note}".strip()
            conn.execute("UPDATE ad_units SET state = ?, status_note = ? WHERE id = ?", (state, note, u["id"]))
    conn.commit()
    await ensure_bids(client, conn, biz["id"], today)   # корректировки по полу и возрасту из профиля аудитории ставит сам сервис
    try:
        await ensure_autotargeting(client, conn, biz["id"], today)   # категории автотаргетинга подбирает и держит в кабинете сам сервис
    except DirectError:
        pass
    try:
        await ensure_traffic(client, conn, biz["id"], today)   # нет показов: сервис сам расширяет охват (категории, более широкие фразы)
    except DirectError:
        pass

    goal =biz["metrika_goal_id"] or None
    all_goals = goal_ids_for_report(conn, biz) if goal else []   # все цели счётчика: не только «обращение», а весь путь посетителя
    start = day_add(today, -(days - 1))
    goal_error = ""
    try:
        rows = await client.ad_stats(start, today, goal_id=goal, goal_ids=all_goals)
    except DirectError as first:
        try:
            if len(all_goals) <= 1:
                raise first from first
            rows = await client.ad_stats(start, today, goal_id=goal)   # Директ не принял часть целей (чужой счётчик, цель удалена): хотя бы «обращение» не теряем
        except DirectError as second:
            goal = None
            goal_error = f"Директ не принял цель Метрики в отчёте, поэтому обращения пока не видны (расход и клики обновлены): {second.full_message()}"
            rows = await client.ad_stats(start, today)   # без цели: расход, показы и клики всё равно нужны
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
        for goal_id, count in (r.get("goals") or {}).items():   # каждая цель по дню и объявлению
            conn.execute(
                "INSERT INTO goal_stats (unit_id, day, goal_id, conversions) VALUES (?,?,?,?) "
                "ON CONFLICT(unit_id, day, goal_id) DO UPDATE SET conversions = excluded.conversions", (unit_id, r["day"], goal_id, count))
        n += 1
    conn.commit()
    return {"units": len(units), "rows": n, "goal_error": goal_error}
