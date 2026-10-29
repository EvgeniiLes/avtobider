"""Арт-директор для ИИ-баннеров: каждому баннеру своя сцена (для A/B нужны разные по смыслу картинки, а не перекраска одной).

Пять «сюжетов» заданы жёстко, чтобы баннеры точно отличались; конкретику (кто в кадре, где, что делает) и решение, нужен ли логотип,
придумывает модель по описанию бизнеса. Если модель недоступна, берётся запасная сцена.
"""
import json

import httpx

FAL_CHAT_URL = "https://fal.run/openrouter/router/openai/v1/chat/completions"

# ключ -> (название для людей, задание сюжета; запасная сцена для модели картинок)
SCENES = {
    "scene_group": ("Компания людей", "3-5 people of the target audience together in a natural setting, for example around a table, "
                    "engaged and smiling, the product or a phone in the centre of their attention"),
    "scene_hand": ("Рука с продуктом", "close-up of one hand holding the product or a phone, nothing else in the frame, shallow depth of field, "
                   "plain calm background, a lot of free space for text"),
    "scene_person": ("Один человек", "one person candidly absorbed in using the product, medium shot, a real unposed moment, natural light"),
    "scene_still": ("Предмет без людей", "the product or a phone alone on a styled surface, no people, a hero still life on a bold contrasting backdrop"),
    "scene_emotion": ("Эмоция крупно", "tight close-up of a face with a genuine strong emotion (laughter, surprise, excitement) while using the product"),
}
ORDER = list(SCENES)


def scene_key(index: int) -> str:
    return ORDER[index % len(ORDER)]


def scene_label(key: str) -> str:
    return SCENES[key][0] if key in SCENES else ""


ART_SYSTEM = (
    "Ты опытный SMM-специалист и арт-директор рекламы малого бизнеса в Яндекс Директе. Тебе дают бизнес, заголовок объявления и "
    "обязательный сюжет баннера. Придумай конкретную сцену для нейросети-художника и реши, нужен ли на баннере логотип. "
    "Верни только JSON-объект {\"audience\": \"кто целевая аудитория этого баннера, коротко по-русски\", "
    "\"scene\": \"описание сцены ПО-АНГЛИЙСКИ, 2-3 предложения: кто в кадре, где, что делает, ракурс, свет, настроение; строго в заданном сюжете\", "
    "\"logo\": true или false, \"logo_reason\": \"почему так, по-русски, одно предложение\"}. "
    "Правила сцены: показывай то, что бизнес правда предлагает, и как клиенты им пользуются; не придумывай факты, цены и бренды; "
    "в кадре нет текста, вывесок, логотипов и значков, ничего «фирменного» не рисуй. Никогда не придумывай интерфейс приложения или сайта: "
    "если в запросе сказано, что скриншот есть, телефон с экраном, повёрнутым к зрителю, обязателен; если скриншота нет, "
    "экран нигде не должен читаться (телефон виден со спины или под углом, экран в тени или погашен, либо в кадре только руки и лица людей). "
    "Сцена должна отличаться от сцен других баннеров этого бизнеса (они перечислены в запросе): другие люди, место, ракурс. "
    "Правила логотипа: ставь логотип (true) только если он читается крупно, а в композиции есть спокойное место под заметный блок; "
    "не ставь (false), если логотип мелкий, сложный или с мелким текстом, если кадр плотный, если это крупный план без места, "
    "или если название и так видно в подписи. Сомневаешься: не ставь."
)


async def art_direction(biz, variant: dict, index: int, others: list[str], logo: dict | None, has_screen: bool,
                        fal_key: str, model: str, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Сцена и решение про логотип для баннера под это объявление. Всегда возвращает результат (при сбое запасной)."""
    key = scene_key(index)
    label, brief = SCENES[key]
    fallback = {"concept": key, "label": label, "audience": "", "scene": brief, "logo": False, "logo_reason": "", "ai": False}
    if not fal_key:
        return fallback
    logo_line = ("Логотипа нет, поле logo всегда false." if logo is None else
                 f"Логотип есть: пропорции {logo.get('ratio', 2.5):.1f}:1, оценка читаемости {logo.get('score') or 'не проверялась'} из 5"
                 + (f", замечания: {logo['note']}" if logo.get("note") else "") + ".")
    user = (f"Бизнес: {biz['kind']}, «{biz['name']}». Описание владельца: {(biz['description'] or '')[:500]}\n"
            + (f"Пожелание к картинкам: {biz['visual_hint']}\n" if _field(biz, "visual_hint") else "")
            + f"Заголовок объявления: {variant['title']}\n"
            + (f"Угол подачи: {variant.get('angle')}\n" if variant.get("angle") else "")
            + f"Обязательный сюжет: {label} — {brief}\n"
            + "Раскладку (где будет текст) не задавай: это решит художник. В сцене оставь спокойную область, на которую можно положить крупный текст.\n"
            + ("Настоящий скриншот есть: телефон с экраном обязателен. Человек держит его в руках ВЕРТИКАЛЬНО (портретная ориентация), экраном к камере, "
               "крупно (экран не меньше 20% высоты кадра). Телефон не лежит на столе и не держится горизонтально.\n" if has_screen
               else "Скриншота приложения нет: экран телефона нигде не должен читаться, интерфейс не придумывай.\n")
            + (f"Сцены других баннеров (не повторяй): {' | '.join(others)}\n" if others else "")
            + logo_line)
    body = {"model": model, "max_tokens": 900, "messages": [{"role": "system", "content": ART_SYSTEM}, {"role": "user", "content": user}]}
    try:
        async with httpx.AsyncClient(timeout=60, transport=transport) as http:
            r = await http.post(FAL_CHAT_URL, json=body, headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"})
        if r.status_code != 200:
            return fallback
        raw = r.json()["choices"][0]["message"]["content"] or ""
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return fallback
    scene = str(data.get("scene") or "").strip()[:700]
    if len(scene) < 30:
        return fallback
    return {"concept": key, "label": label, "audience": str(data.get("audience") or "")[:120], "scene": scene,
            "logo": bool(data.get("logo")) and logo is not None, "logo_reason": str(data.get("logo_reason") or "")[:200], "ai": True}


REVIEW_SYSTEM = (
    "Ты строгий арт-директор. Оцени готовый рекламный баннер и верни только JSON-объект "
    "{\"score\": число от 1 до 5, \"issues\": [\"...\"]}. Оценка: 5 отлично; 3 приемлемо; 1-2 нельзя выпускать. "
    "Снижай оценку за: уродливые или искажённые руки, лица и пальцы, лишние предметы, не соответствующие описанию бизнеса, "
    "нечитаемый или мелкий текст, текст налезает на объект, шаблонный вид «сделано нейросетью» (лишний блеск, золотая пыль, рамки с орнаментом), "
    "нарисованный нейросетью логотип, эмблема, значок или фирменный знак, придуманный интерфейс приложения на экране телефона "
    "(такие баннеры оценивай не выше 2; исключения: экран заведомо не читается, либо в задании сказано, что на экране настоящий скриншот владельца: "
    "тогда интерфейс на экране не оценивай вообще), "
    "пустой или перегруженный кадр, логотип или зелёный/пурпурный прямоугольник-заглушка, оставшийся не закрашенным. "
    "В issues коротко по-русски перечисли реальные проблемы (не больше трёх). Если проблем нет, issues пустой."
)


async def review_banner(image_url: str, brief: str, fal_key: str, model: str,
                        transport: httpx.AsyncBaseTransport | None = None) -> dict | None:
    """Самопроверка готового баннера моделью со зрением. None, если оценить не удалось (тогда баннер не отбраковываем)."""
    body = {"model": model, "max_tokens": 500, "image_urls": [image_url],
            "messages": [{"role": "system", "content": REVIEW_SYSTEM}, {"role": "user", "content": f"Бизнес и задание: {brief[:1200]}"}]}
    try:
        async with httpx.AsyncClient(timeout=90, transport=transport) as http:
            r = await http.post(FAL_CHAT_URL, json=body, headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"})
        if r.status_code != 200:
            return None
        raw = r.json()["choices"][0]["message"]["content"] or ""
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        score = max(1, min(5, int(data.get("score"))))
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        return None
    return {"score": score, "issues": [str(i)[:120] for i in (data.get("issues") or []) if i][:3]}


def _field(biz, name: str) -> str:
    try:
        return (biz[name] or "").strip()
    except (KeyError, IndexError):
        return ""
