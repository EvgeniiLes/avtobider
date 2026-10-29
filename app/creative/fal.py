"""Генерация картинок и видео через fal.ai (один ключ, модели меняются настройками).

Работа через очередь: отправили задание, спрашиваем готовность, забираем результат.
Названия моделей лежат в настройках (FAL_IMAGE_MODEL, FAL_VIDEO_MODEL), поэтому модель можно сменить без правки кода.
"""
import httpx

QUEUE = "https://queue.fal.run"
# Примерная стоимость в центах доллара: нужна, чтобы не выйти за лимит расходов на генерацию.
COST_CENTS = {"text": 1, "image": 8, "video": 70, "ai_banner": 7, "check": 1, "logo_cut": 1}  # ai_banner: GPT Image 2 (4:3, среднее качество) 3.7 цента, арт-директор, проверка текста и самопроверка по 1 центу


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
    description = (biz["description"] or "").strip()[:500]
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


def banner_prompt(biz, title: str, price: str, cta: str, logo_corner: bool = False, index: int = 0, screen: bool = False,
                  logo_ratio: float | None = None, scene: str = "") -> str:
    """Запрос на баннер целиком: русский текст пишет сама модель, поэтому он короткий и в точных кавычках.
    scene: сцена от арт-директора (кто в кадре и что делает); direction задаёт только оформление: цвета, шрифт, компоновку."""
    text = f'headline «{title}»'
    if price:
        text += f' and a price badge «{price.replace(chr(160), " ")}»'
    return (f"Create a polished, professional advertising banner for Yandex Direct, landscape 4:3. Business: {biz['kind']}; the brand name "
            f"«{biz['name']}» is NOT written on the banner. " + _visual_rules(biz)
            + f"Scene, follow it closely: {scene or 'one clear subject that shows what the business really offers'}. "
            "One clear focal subject, natural realistic photography, correct anatomy and hands, no clutter. "
            "Choose the composition freely like an art director (the text may sit on the left, right, top or bottom), but the text always lies on "
            "a calm area with strong contrast and never covers faces or the main subject. "
            f"Text in Russian, spelled EXACTLY as given, nothing added, nothing changed: {text}, and below it a solid rounded button «{cta}». "
            "The headline is very large, heavy bold sans-serif, left aligned, at most 4 lines, the first thing the eye sees, with strong contrast "
            "against its background. "
            "Visual direction: " + DIRECTIONS[index % len(DIRECTIONS)] + ". A harmonious palette of 2-3 colors; it looks like an ad from a top agency. "
            "Never add: any other text, website addresses, domain names, phone numbers, logos, emblems, icons, mascots, watermarks; "
            "an invented app or website interface (a phone in the scene shows a plain dark screen or is turned away); "
            "decorative frames, ornaments, golden dust, sparkles, glow or lens flare."
            + (LOGO_RULE.format(ratio=logo_ratio or 2.5, width=36 if (logo_ratio or 2.5) >= 3 else 28) if logo_corner else "")
            + (SCREEN_RULE if screen else ""))

async def _submit(http: httpx.AsyncClient, model: str, key: str, payload: dict) -> dict:
    try:
        r = await http.post(f"{QUEUE}/{model}", json=payload, headers=_headers(key))
    except httpx.HTTPError:
        raise FalError("Не удалось связаться с сервисом генерации. Попробуйте позже.")
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


def video_payload(model: str, image_url: str, prompt: str) -> dict:
    if "kling-video/v3" in model or "kling-video/o3" in model:
        return {"start_image_url": image_url, "prompt": prompt, "duration": "5", "generate_audio": False}  # звук дороже и в Директе не нужен
    if "grok-imagine-video" in model:
        return {"image_url": image_url, "prompt": prompt, "duration": 6, "resolution": "720p", "aspect_ratio": "auto"}
    return {"image_url": image_url, "prompt": prompt, "duration": "5"}


async def start_image(prompt: str, key: str, model: str, transport=None) -> dict:
    async with httpx.AsyncClient(timeout=60, transport=transport) as http:
        return await _submit(http, model, key, image_payload(model, prompt))


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
