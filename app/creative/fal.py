"""Генерация картинок и видео через fal.ai (один ключ, модели меняются настройками).

Работа через очередь: отправили задание, спрашиваем готовность, забираем результат.
Названия моделей лежат в настройках (FAL_IMAGE_MODEL, FAL_VIDEO_MODEL), поэтому модель можно сменить без правки кода.
"""
import re

import httpx

QUEUE = "https://queue.fal.run"
# Примерная стоимость в центах доллара: нужна, чтобы не выйти за лимит расходов на генерацию.
COST_CENTS = {"text": 1, "image": 8, "video": 70, "ai_banner": 7, "check": 1, "logo_cut": 1, "strategist": 3, "ref_extra": 2}   # ref_extra: вторая картинка (референс) на входе баннера  # ai_banner: GPT Image 2 (4:3, среднее качество) 3.7 цента, арт-директор, проверка текста и самопроверка по 1 центу; сверка с фактами (текстовая, меньше цента) в цену не добавлена


class FalError(Exception):
    """Понятная пользователю причина, почему картинка или видео не получились."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _headers(key: str) -> dict:
    return {"Authorization": f"Key {key}", "Content-Type": "application/json"}


# Пять разных «характеров» кадра: для сравнения (A/B) нужны не копии, а разные подходы. Каждый подходит под свой шаблон баннера.
PHOTO_STYLES = [
    "Крупный план главного: результат услуги или сам товар в центре кадра, мелкая глубина резкости, чистый светлый фон",
    "Тёмный эффектный кадр: тёмный фон, мягкий контрастный свет, премиальное настроение, золотистые блики",
    "Вид сверху: аккуратная раскладка предметов по теме услуги на нежном пастельном однотонном фоне, без людей",
    "Яркий насыщенный студийный кадр: сочные цвета, динамичная композиция, однотонный цветной фон",
    "Светлое помещение, мастер и клиент за работой, живые улыбки, естественный дневной свет",
]


def _visual_rules_ru(biz) -> str:
    try:
        hint = (biz["visual_hint"] or "").strip()
    except (KeyError, IndexError):
        hint = ""
    text = ("Рисуй ровно то, что бизнес предлагает на самом деле, без физических аналогов: если услуга онлайн или в телефоне, "
            "покажи смартфон или экран приложения, а не реальные предметы.")
    return text + (f" Пожелание владельца, выполни строго: {hint}." if hint else "")


def image_prompt(biz, idea: str = "", style: int = 0) -> str:
    """Запрос к генератору картинок: без текста на изображении (текст и логотип накладываем сами).
    style выбирает один из пяти непохожих кадров, чтобы варианты рекламы различались."""
    first = (biz["description"] or "").strip().split(".")[0][:200]
    base = f"Рекламная фотография в стиле коммерческой съёмки: {biz['kind']}. " + _visual_rules_ru(biz)
    if first:
        base += f". {first}"
    if idea.strip():
        base += f". {idea.strip()[:300]}"
    base += ". " + PHOTO_STYLES[style % len(PHOTO_STYLES)]
    return (base + ". Аккуратная композиция, объект сбоку, с одной стороны чистое место для текста. "
            "Без текста, букв, логотипов, узнаваемых брендов на упаковке и водяных знаков.")


# Только оформление: цвета и настроение. Где стоит текст и как скомпонован кадр, решает сама модель, чтобы варианты отличались и для A/B.
DIRECTIONS = (
    "airy light look: warm white and soft daylight, dark navy headline, one calm accent color on the button",
    "dark contrast look: near-black background, white headline, ONE bright accent color (orange or cyan, never green) on the button, "
    "the subject lit with a soft rim light",
    "bold colorful look: one saturated color field (royal blue, violet or red), white headline, yellow button",
    "soft pastel look: gentle peach, mint or lilac tones, dark headline, rounded button",
    "warm photographic look: a full-bleed photo with warm light, text placed on a smooth darker area of the photo, white headline",
)


def _visual_rules(biz) -> str:
    """Общие правила для картинок: рисовать то, что бизнес правда предлагает, и слушаться пожеланий владельца."""
    try:
        hint = (biz["visual_hint"] or "").strip()
    except (KeyError, IndexError):
        hint = ""
    from app.creative.angles import effective_description  # здесь, чтобы модули не импортировали друг друга по кругу
    description = effective_description(biz).strip()[:500]
    rules = (f"About the business (use only this, do not invent anything): {description}. "
             "Depict exactly what the business really offers and how clients use it. Never replace the real product with a physical "
             "equivalent: if the description says it is online or used on a phone, show people with a smartphone, not real-world objects. ")
    if hint:
        rules += f"The owner's direction for the visuals, follow it strictly: {hint}. "
    return rules


SCREEN_RULE = (" The scene includes a modern smartphone held in a hand, upright in portrait orientation (vertical, never lying on a table, never horizontal), "
               "its screen facing the viewer and clearly visible (the screen at least 20% of the banner height), slightly angled. IMPORTANT: the phone screen must be one completely flat, solid, pure "
               "magenta (#FF00FF) rectangle filling the whole screen, with no interface, no text, no reflections, no gradient. "
               "Do not use magenta or hot pink anywhere else in the banner.")


LOGO_RULE = (" The company logo is part of the layout: reserve one clean, flat, solid pure green (#00FF00) rectangle for it, width to height "
             "about {ratio:.1f} to 1, completely empty inside (no text, no letters, no gradient), at least {width}% of the banner width, on a calm plain area "
             "of the background with a clear margin of at least 4% of the banner width from the headline, the button and the edges. "
             "Choose the place yourself so that it looks designed, for example next to the headline or in a lower corner, not squeezed into an edge. "
             "Do not use green anywhere else in the banner.")


# Общие блоки промптов баннера (приёмы из сильных промптов для GPT Image 2: цель первой, текст «пришит» кавычками, одна картинка,
# конкретный шрифт, место под текст, закрывающий блок «избегать» с причинами).
ONE_BANNER = "Generate only ONE finished banner (not a mockup, not a collage, not a presentation board). "
AVOID = ("Avoid: any other text or invented words, the business name, website addresses, domain names, phone numbers, logos, emblems, icons, mascots, "
         "watermarks; decorative frames, ornaments, golden dust, sparkles, glow or lens flare (they make the banner look cheap); 3D or glossy lettering "
         "effects (the text must stay flat and readable); distorted hands or extra fingers, plastic skin, warped, blurred or duplicated letters.")


_URLISH = re.compile(r"https?://|www\.|\.(?:ru|com|net|org|io|рф)\b", re.IGNORECASE)


def caption_parts(biz) -> list[str]:
    """Подпись внизу баннера: название бизнеса и вид деятельности. Без неё по заголовку вроде «Пилот на 30 дней» не понять,
    кто и что рекламирует. Подпись дорисовывает на готовом баннере код (compose.add_caption), а не нейросеть: так она всегда читается
    и без искажённых букв. Адрес сайта в подпись не берём (проверка баннера такие надписи бракует), слишком длинное тоже."""
    parts: list[str] = []
    for value, limit in ((biz["name"], 28), (biz["kind"], 52)):
        value = re.sub(r"\s+", " ", value or "").strip()
        if value and len(value) <= limit and not _URLISH.search(value) and value.lower() not in {p.lower() for p in parts}:
            if parts and _stems(value) and _stems(value) <= _stems(parts[0]):   # вид деятельности уже сказан в названии («Студия маникюра «Лилия»» и «Маникюрная студия»): повтор путает
                continue
            parts.append(value)
    return parts


def _stems(text: str) -> set[str]:
    """Основы слов (первые пять букв): по ним видно, что «маникюра» и «маникюрная» говорят об одном."""
    return {w[:5] for w in re.findall(r"[а-яёa-z]{4,}", text.lower())}


def caption_for(biz) -> str:
    return " · ".join(caption_parts(biz))


def _text_block(title: str, price: str, cta: str, by_direction: bool = False) -> str:
    """Что написано на баннере: точные строки в кавычках и как их набирать. Короткие русские фразы модель держит стабильнее длинных.
    by_direction: размер, регистр и место заголовка задаёт направление (приём референса вроде гигантского слова), общий шрифт не навязываем."""
    shown = f"headline «{title}»"
    if price and re.sub(r"\D", "", price) not in re.sub(r"\D", "", title):   # цена уже названа в заголовке: отдельную плашку не рисуем
        shown += f", price badge «{price.replace(chr(160), ' ')}»"
    style = ("The headline's size, case, weight and placement follow the Direction exactly; the letters stay flat, bold and perfectly legible, "
             "with strong contrast against their background. " if by_direction else
             "The headline is heavy bold sans-serif with tight letter spacing, very large, left aligned, at most 4 lines, the first thing the eye sees, "
             "with strong contrast against its background. ")
    tail = "Keep the bottom 8% of the banner calm: no text and no button there, a caption strip is added at the bottom edge later. "
    return ("Text in Russian, spelled EXACTLY as written between the quotes: not translated, not abbreviated, no letter added or lost, and no other large text "
            f"anywhere. {style}Text to show: {shown}, and a solid rounded button «{cta}». " + tail)


def banner_prompt(biz, title: str, price: str, cta: str, logo_corner: bool = False, index: int = 0, screen: bool = False,
                  logo_ratio: float | None = None, scene: str = "", direction: str = "") -> str:
    """Запрос на баннер целиком: русский текст пишет сама модель, поэтому он короткий и в точных кавычках.
    scene: сцена от арт-директора (кто в кадре и что делает); direction задаёт только оформление: цвета, шрифт, компоновку.
    direction (свой текст) заменяет оформление из списка: так подача по референсу работает и без фото владельца."""
    return (f"Purpose: one polished advertising banner for Yandex Direct, landscape 4:3. Business: {biz['kind']}; the brand name "
            f"«{biz['name']}» is NOT written on the banner. " + _visual_rules(biz)
            + f"Scene, follow it closely: {scene or 'one clear subject that shows what the business really offers'}. "
            "One clear focal subject, natural realistic photography with natural skin texture, correct anatomy and hands, no clutter. "
            "People are busy with real work and their faces are calm and natural: no theatrical surprise or shock (wide-open eyes and mouth), "
            "no show-off delight, no stock-photo poses. "
            "Choose the composition freely like an art director (the text may sit on the left, right, top or bottom), but the text always lies on "
            "a calm area with strong contrast and never covers faces or the main subject. "
            + _text_block(title, price, cta)
            + "Visual direction: " + (direction or DIRECTIONS[index % len(DIRECTIONS)]) + ". A harmonious palette of 2-3 colors; it looks like an ad from a top agency. "
            + ONE_BANNER + AVOID
            + " Also forbidden: an invented app or website interface (a phone in the scene shows a plain dark screen or is turned away)."
            + (LOGO_RULE.format(ratio=logo_ratio or 2.5, width=36 if (logo_ratio or 2.5) >= 3 else 28) if logo_corner else "")
            + (SCREEN_RULE if screen else ""))


def _owner_hint(biz) -> str:
    """Пожелание владельца к картинкам. Описание бизнеса в промпт по референсу не кладём: модель переписывала его слова на баннер."""
    try:
        hint = (biz["visual_hint"] or "").strip()
    except (KeyError, IndexError):
        hint = ""
    return f"The owner's direction for the visuals, follow it strictly: {hint}. " if hint else ""


TOPUP_MESSAGE = ("На счёте сервиса генерации (fal.ai) закончились деньги, поэтому ИИ, картинки и видео сейчас не работают. "
                 "Пополните счёт на fal.ai (раздел Billing): после этого всё заработает без других действий.")
CHAT_URL = "https://fal.run/openrouter/router/openai/v1/chat/completions"


def is_locked(status_code: int, text: str) -> bool:
    """Ответ fal.ai «аккаунт заблокирован: нужно пополнить счёт» (403 с причиной TOP_UP)."""
    return status_code in (402, 403) and any(w in (text or "").upper() for w in ("TOP_UP", "LOCKED", "BALANCE"))


async def why_down(key: str, model: str, transport=None) -> str:
    """Почему ИИ не отвечает: если закончились деньги на счёте fal.ai, возвращает понятное сообщение, иначе пустую строку (причина другая).
    Проверка лёгкая: один запрос на один токен, который выполняется только когда обычный запрос уже не удался."""
    if not key:
        return ""
    body = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ok"}]}
    try:
        async with httpx.AsyncClient(timeout=30, transport=transport) as http:
            r = await http.post(CHAT_URL, json=body, headers=_headers(key))
    except httpx.HTTPError:
        return ""
    return TOPUP_MESSAGE if is_locked(r.status_code, r.text) else ""


async def _submit(http: httpx.AsyncClient, model: str, key: str, payload: dict) -> dict:
    try:
        r = await http.post(f"{QUEUE}/{model}", json=payload, headers=_headers(key))
    except httpx.HTTPError:
        raise FalError("Не удалось связаться с сервисом генерации. Попробуйте позже.")
    if is_locked(r.status_code, r.text):
        raise FalError(TOPUP_MESSAGE)
    if r.status_code in (401, 403):
        raise FalError("Ключ сервиса генерации не подошёл. Администратору нужно проверить FAL_KEY.")
    if r.status_code == 402 or r.status_code == 429:
        raise FalError("Сервис генерации сейчас недоступен: закончились средства или слишком много запросов.")
    if r.status_code >= 400:
        raise FalError("Сервис генерации не принял задание. Попробуйте изменить пожелание.")
    return r.json()


def image_payload(model: str, prompt: str) -> dict:
    """Параметры у моделей разные (схемы сверены по каталогу fal): подбираем под выбранную."""
    if "gpt-image" in model:
        return {"prompt": prompt, "image_size": "landscape_4_3", "quality": "medium", "num_images": 1, "output_format": "jpeg"}
    if "seedream" in model:   # у Seedream 5 Pro минимум 921 600 пикселей, поэтому размер задаём числами (4:3)
        return {"prompt": prompt, "image_size": {"width": 1280, "height": 960}, "num_images": 1, "output_format": "jpeg"}
    if "nano-banana" in model:
        return {"prompt": prompt, "aspect_ratio": "4:3", "resolution": "1K", "num_images": 1, "output_format": "jpeg"}
    return {"prompt": prompt, "image_size": "square_hd", "num_images": 1}  # FLUX и похожие


# Баннер по настоящему фото владельца: модель получает фото и промпт про оформление. Оформления разные, чтобы варианты отличались для A/B.
PHOTO_LOOKS = (
    ("светлое", "airy light look: warm white and cream tones; the photo is large (you may extend its background to the whole banner); "
                "dark headline on a calm area that never covers the main subject; a soft accent button"),
    ("яркое", "bold colorful look: one saturated color field (turquoise, royal blue or violet); the photo unchanged inside a large rounded-corner "
              "frame on one side; white heavy bold headline; yellow rounded button"),
    ("тёмное", "dark premium-contrast look: deep charcoal; the photo full-bleed with a smooth dark gradient on one side for the text; "
               "white headline; one bright accent color (orange or cyan, never gold) on the button"),
    ("пастельное", "soft pastel look: gentle peach, mint or lilac background; the photo in a rounded or arch shape on one side; "
                   "dark headline; rounded pastel button"),
    ("фото во весь баннер", "photo-hero look: the photo fills the whole banner, the headline in large white letters on a darkened smooth "
                            "gradient area of the photo, the button below it"),
)


def photo_banner_prompt(biz, title: str, price: str, cta: str, index: int = 0, mood: str = "") -> str:
    """Запрос «сделай баннер из этого фото»: фото владельца остаётся настоящим, меняется только оформление и текст."""
    name, look = PHOTO_LOOKS[index % len(PHOTO_LOOKS)]
    return ("Purpose: one advertising banner for Yandex Direct, landscape 4:3, for a Russian audience "
            f"(the business is: {biz['kind']}; context only, never write it on the banner). "
            "Subject: the attached photo is the one and only reference of the subject. Preserve exactly the same people, animals, objects and place, "
            "their faces, coat or clothing and proportions, so the real photo stays natural and unaltered; change only the layout, the crop, "
            "the extended background and the colors around it. "
            f"Look and composition: Visual direction: {look}. " + (f"Audience and mood: {mood}. " if mood else "")
            + "The main subject keeps about half of the frame; the text sits in reserved negative space and never covers faces or the main subject. "
            + _text_block(title, price, cta) + ONE_BANNER + AVOID)


def reference_banner_prompt(biz, title: str, price: str, cta: str, direction: str) -> str:
    """Баннер из фото владельца по мотивам референса: вторая картинка служит только вдохновением, её не копируем."""
    return ("Purpose: one advertising banner for Yandex Direct, landscape 4:3, for a Russian audience "
            f"(the business is: {biz['kind']}; context only, never write it on the banner). "
            "Subject: the FIRST attached image is the business's real photo and the one and only reference of the subject: preserve exactly the same people, "
            "animals, objects and place, their faces, coat or clothing and proportions, keep it natural and unaltered; you may crop it, extend its background "
            "and place it freely in the layout. "
            "Layout: the SECOND attached image is the design reference. Follow its layout closely: the same composition and framing, the same size, weight, case and position "
            "of the headline (including a huge headline that overlaps the subject), the same color palette, panels, overlay and frame effects. Put the business photo "
            "(the FIRST image) into that layout as its main subject. Do NOT reuse the reference's own text, small print, lists, badges, logo, products, animals or "
            "characters, and do not copy its photo: only the design. "
            f"Look and composition: Direction: {direction}. The text never covers faces or the main subject. "
            + _text_block(title, price, cta, by_direction=True) + ONE_BANNER + AVOID)


def edit_model(model: str) -> str:
    """Модель, которая умеет принимать фото: для GPT Image это тот же GPT Image в режиме edit."""
    if model.endswith("/edit"):
        return model
    return (model if "gpt-image" in model else "openai/gpt-image-2") + "/edit"


def edit_payload(prompt: str, image_urls: list[str]) -> dict:
    return {"prompt": prompt, "image_urls": image_urls, "image_size": "landscape_4_3", "quality": "medium", "num_images": 1, "output_format": "jpeg"}


def video_payload(model: str, image_url: str, prompt: str) -> dict:
    if "kling-video/v3" in model or "kling-video/o3" in model:
        return {"start_image_url": image_url, "prompt": prompt, "duration": "5", "generate_audio": False}  # звук дороже и в Директе не нужен
    if "grok-imagine-video" in model:
        return {"image_url": image_url, "prompt": prompt, "duration": 6, "resolution": "720p", "aspect_ratio": "auto"}
    return {"image_url": image_url, "prompt": prompt, "duration": "5"}


async def start_image(prompt: str, key: str, model: str, transport=None) -> dict:
    async with httpx.AsyncClient(timeout=60, transport=transport) as http:
        return await _submit(http, model, key, image_payload(model, prompt))


async def start_edit(prompt: str, image_urls: list[str], key: str, model: str, transport=None) -> dict:
    async with httpx.AsyncClient(timeout=90, transport=transport) as http:
        return await _submit(http, edit_model(model), key, edit_payload(prompt, image_urls))


async def start_video(image_url: str, prompt: str, key: str, model: str, transport=None) -> dict:
    async with httpx.AsyncClient(timeout=60, transport=transport) as http:
        return await _submit(http, model, key, video_payload(model, image_url, prompt))


async def poll(job: dict, key: str, transport=None) -> tuple[str, str]:
    """Состояние задания: ('working', ''), ('done', адрес файла) или ('failed', причина)."""
    async with httpx.AsyncClient(timeout=60, transport=transport) as http:
        try:
            status = (await http.get(job["status_url"], headers=_headers(key))).json()
            if status.get("status") in ("IN_QUEUE", "IN_PROGRESS"):
                return "working", ""
            result = (await http.get(job["response_url"], headers=_headers(key))).json()
        except (httpx.HTTPError, ValueError, KeyError):
            return "working", ""  # временный сбой: спросим ещё раз
    if status.get("error"):
        return "failed", "Генератор не справился с заданием."
    url = ((result.get("video") or {}).get("url") or (result.get("image") or {}).get("url")
           or next((i.get("url") for i in result.get("images", []) if i.get("url")), ""))
    return ("done", url) if url else ("failed", "Генератор не вернул результат.")


async def download(url: str, transport=None) -> bytes:
    async with httpx.AsyncClient(timeout=120, transport=transport, follow_redirects=True) as http:
        try:
            r = await http.get(url)
        except httpx.HTTPError:
            raise FalError("Не удалось скачать результат генерации.")
    if r.status_code != 200:
        raise FalError("Не удалось скачать результат генерации.")
    return r.content
