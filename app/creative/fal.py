"""Генерация картинок и видео через fal.ai (один ключ, модели меняются настройками).

Работа через очередь: отправили задание, спрашиваем готовность, забираем результат.
Названия моделей лежат в настройках (FAL_IMAGE_MODEL, FAL_VIDEO_MODEL), поэтому модель можно сменить без правки кода.
"""
import httpx

QUEUE = "https://queue.fal.run"
# Примерная стоимость в центах доллара: нужна, чтобы не выйти за лимит расходов на генерацию.
COST_CENTS = {"text": 1, "image": 8, "video": 70, "ai_banner": 16, "check": 1}  # ai_banner: Nano Banana Pro 15 центов и проверка текста


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


def image_prompt(biz, idea: str = "", style: int = 0) -> str:
    """Запрос к генератору картинок: без текста на изображении (текст и логотип накладываем сами).
    style выбирает один из пяти непохожих кадров, чтобы варианты рекламы различались."""
    first = (biz["description"] or "").strip().split(".")[0][:200]
    base = f"Рекламная фотография в стиле коммерческой съёмки: {biz['kind']}"
    if first:
        base += f". {first}"
    if idea.strip():
        base += f". {idea.strip()[:300]}"
    base += ". " + PHOTO_STYLES[style % len(PHOTO_STYLES)]
    return (base + ". Аккуратная композиция, объект сбоку, с одной стороны чистое место для текста. "
            "Без текста, букв, логотипов, узнаваемых брендов на упаковке и водяных знаков.")


def banner_prompt(biz, title: str, price: str, cta: str, logo_corner: bool = False) -> str:
    """Запрос на баннер целиком: русский текст пишет сама модель, поэтому он короткий и в точных кавычках."""
    first = (biz["description"] or "").strip().split(".")[0][:160]
    parts = [f'headline "{title}"']
    if price:
        parts.append(f'price badge "{price.replace(chr(160), " ")}"')
    parts += [f'button "{cta}"', f'small caption "{biz["name"]}"']
    return (f"Advertising banner for a business: {biz['kind']}. {first}. Landscape 4:3, modern professional layout with a real-looking "
            "photo relevant to the business on one side, harmonious soft palette, lots of clean space around the text. "
            "Text in Russian, exactly as written, large and perfectly legible: " + ", ".join(parts) + ". "
            "No other text, no extra letters, no third-party brand logos."
            + (" Keep the top-right corner plain and empty: a logo will be placed there." if logo_corner else ""))


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
    url = (result.get("video") or {}).get("url") or next((i.get("url") for i in result.get("images", []) if i.get("url")), "")
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
