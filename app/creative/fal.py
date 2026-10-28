"""Генерация картинок и видео через fal.ai (один ключ, модели меняются настройками).

Работа через очередь: отправили задание, спрашиваем готовность, забираем результат.
Названия моделей лежат в настройках (FAL_IMAGE_MODEL, FAL_VIDEO_MODEL), поэтому модель можно сменить без правки кода.
"""
import httpx

QUEUE = "https://queue.fal.run"
# Примерная стоимость в центах доллара: нужна, чтобы не выйти за лимит расходов на генерацию.
COST_CENTS = {"text": 1, "image": 8, "video": 70}


class FalError(Exception):
    """Понятная пользователю причина, почему картинка или видео не получились."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _headers(key: str) -> dict:
    return {"Authorization": f"Key {key}", "Content-Type": "application/json"}


def image_prompt(biz, idea: str = "") -> str:
    """Запрос к генератору картинок: без текста на изображении (текст и логотип накладываем сами)."""
    first = (biz["description"] or "").strip().split(".")[0][:200]
    base = f"Реалистичная фотография для рекламы: {biz['kind']}"
    if first:
        base += f". {first}"
    if idea.strip():
        base += f". {idea.strip()[:300]}"
    return (base + ". Тёплый естественный свет, аккуратная композиция, свободное место для заголовка. "
            "Без текста, букв, логотипов, узнаваемых брендов на упаковке и водяных знаков.")


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
