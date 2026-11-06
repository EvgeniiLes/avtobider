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
                    "engaged and smiling, the product itself in the centre of their attention"),
    "scene_hand": ("Рука с продуктом", "close-up of one hand holding the product, nothing else in the frame, shallow depth of field, "
                   "plain calm background, a lot of free space for text"),
    "scene_person": ("Один человек", "one person candidly absorbed in using the product, medium shot, a real unposed moment, natural light"),
    "scene_still": ("Предмет без людей", "the product alone on a styled surface, no people, a hero still life on a bold contrasting backdrop"),
    "scene_emotion": ("Эмоция крупно", "tight close-up of a face with a genuine strong emotion (laughter, surprise, excitement) while using the product"),
}
ORDER = list(SCENES)


def scene_key(index: int) -> str:
    return ORDER[index % len(ORDER)]


def scene_label(key: str) -> str:
    return SCENES[key][0] if key in SCENES else ""


# Проверенные композиции рекламы (из разбора референсов, рефы/README.md): когда референса нет, баннер строится по одной из них, а не «как получится».
# ключ -> (название для людей, описание композиции для модели)
LAYOUTS = {
    "giant_word": ("Огромное слово", "a flat saturated single-color background; the headline is set in giant ultra-condensed heavy capitals stacked on the left and partly "
                   "bleeding off the edge; the hero subject sits on the right and slightly overlaps the letters for depth; the button sits below the headline"),
    "still_life": ("Предмет-герой", "a seamless saturated studio backdrop with one hero subject lit like a product shot and placed low on one side; a large calm empty area "
                   "above and beside it holds the heavy bold headline, the button right under it"),
    "color_split": ("Цветная плашка и кадр", "the canvas is split in two: one half is a flat saturated color field with the headline and the button, the other half is a large "
                    "rounded-corner picture of the hero subject"),
    "metaphor": ("Игра образом", "a playful, witty still life that shows the service through an unexpected but clean visual metaphor on a plain pastel backdrop; the headline "
                 "sits in clear space at the top, the button under it"),
    "hand": ("Рука с предметом", "a close-up of one hand holding the key tool or product on a plain bold-color background, shallow depth of field; a short headline and "
            "the button on the opposite side"),
    "dark": ("Тёмный контраст", "a deep charcoal full-bleed hero image with a smooth dark gradient on one side for a white headline; one bright accent color on the button"),
    "airy": ("Светлый воздух", "warm white and cream tones, the hero subject large and bright, a dark headline on a calm area and a soft accent button"),
    "arch": ("Пастельная арка", "a soft pastel background, the hero subject inside a rounded or arch-shaped frame on one side, a dark headline and a rounded pastel button"),
}
LAYOUT_ORDER = list(LAYOUTS)


TEXT_HEAVY = ("color_split", "dark", "airy", "arch")        # много места под текст: плашка или спокойная зона
TEXT_LIGHT = ("giant_word", "still_life", "metaphor", "hand")   # текст короткий, на баннере главное картинка или одно слово
FOOD_KINDS = ("кофе", "кафе", "пекар", "ресторан", "пицц", "суши", "кондитер", "бар", "столов", "еда", "доставк")
FOOD_LAYOUTS = ("still_life", "color_split", "airy", "dark", "arch")


def layout_for(index: int, headline: str = "", kind: str = "", has_price: bool = False) -> tuple[str, str, str]:
    """Композиция для баннера без референса. Выбор зависит от текста на баннере и бизнеса, а при равных вариантах у соседних объявлений разные.
    Длинный текст или цена: широкая плашка под текст. Короткий (1-2 слова): картинка или одно огромное слово. Еда: предмет-герой, плашка, светлый фон.
    Возвращает (ключ, название, описание)."""
    text = (headline or "").strip()
    words = text.split()
    if not text:
        pool = list(LAYOUT_ORDER)
    elif len(text) >= 30 or len(words) >= 5 or has_price:
        pool = list(TEXT_HEAVY)
    elif len(words) <= 2 and len(text) <= 18:
        pool = [k for k in TEXT_LIGHT if k != "giant_word" or max(len(w) for w in words) <= 10] + ["arch"]
    else:
        pool = [k for k in LAYOUT_ORDER if k != "giant_word"]
    if any(w in (kind or "").lower() for w in FOOD_KINDS):
        pool = [k for k in pool if k in FOOD_LAYOUTS] or list(FOOD_LAYOUTS)
    key = pool[index % len(pool)]
    return key, LAYOUTS[key][0], LAYOUTS[key][1]


def palette_hint(biz) -> str:
    """Цвета бизнеса для баннера без референса: иначе палитру выбирает модель и она получается случайной."""
    main, extra = _field(biz, "color_main"), _field(biz, "color_extra")
    if main and extra:
        return f"Brand palette: dominant color {main}, secondary color {extra}; keep the rest neutral."
    if main:
        return f"Brand palette: dominant color {main}; keep the rest neutral."
    return "Palette: 2-3 harmonious colors, one saturated accent."


ART_SYSTEM = (
    "Ты опытный SMM-специалист и арт-директор рекламы малого бизнеса в Яндекс Директе. Тебе дают бизнес, заголовок объявления и "
    "обязательный сюжет баннера. Придумай конкретную сцену для нейросети-художника и реши, нужен ли на баннере логотип. "
    "Верни только JSON-объект {\"audience\": \"кто целевая аудитория этого баннера, коротко по-русски\", "
    "\"scene\": \"описание сцены ПО-АНГЛИЙСКИ, 2-3 предложения: кто в кадре, где, что делает, ракурс, свет, настроение; строго в заданном сюжете\", "
    "\"logo\": true или false, \"logo_reason\": \"почему так, по-русски, одно предложение\"}. "
    "Правила сцены: показывай то, что бизнес правда предлагает, и как клиенты им пользуются; не придумывай факты, цены и бренды; "
    "в кадре нет текста, вывесок, логотипов и значков, ничего «фирменного» не рисуй. Люди заняты делом, выражение лица спокойное и естественное: "
    "без театрального удивления и шока (широко раскрытые глаза и рот), без показного восторга и поз как на стоковых фото; не используй "
    "стоковые метафоры вроде цепи, шестерёнок, светящегося мозга и рукопожатия над ноутбуком: показывай саму услугу или её результат. Никогда не придумывай интерфейс приложения или сайта: "
    "если в запросе сказано, что скриншот есть, телефон с экраном, повёрнутым к зрителю, обязателен; если скриншота нет, "
    "экран нигде не должен читаться (телефон виден со спины или под углом, экран в тени или погашен, либо в кадре только руки и лица людей). "
    "Телефон в кадре допустим только если бизнес сам онлайн-услуга или приложение; у офлайн-бизнеса (кофейня, салон, магазин, студия) телефона быть не должно: рисуй продукт, место и людей. "
    "Сцена должна отличаться от сцен других баннеров этого бизнеса (они перечислены в запросе): другие люди, место, ракурс. "
    "Правила логотипа: ставь логотип (true) только если он читается крупно, а в композиции есть спокойное место под заметный блок; "
    "не ставь (false), если логотип мелкий, сложный или с мелким текстом, если кадр плотный, если это крупный план без места, "
    "или если название и так видно в подписи. Сомневаешься: не ставь."
)


SMART_SYSTEM = (
    "You are an art director for small-business ads. You see two images: the FIRST is the business's real photo, the SECOND is a design reference of an ad "
    "whose layout must be reused. Write instructions for an image-editing model on how to fit the business photo into the reference's layout: which part of the "
    "photo to keep and how to crop or extend it, how large the main subject must be and where it sits in the frame (position, overlap with the headline, if the "
    "reference has a huge headline behind the subject), what the background around it should look like to match the reference's palette, and where the calm area "
    "for the headline stays. Keep the people, objects and place of the real photo exactly as they are. Do not mention any text content, brand or contacts. "
    "Answer only a JSON object {\"placement\": \"3-5 short English sentences\"}."
)


def _small_url(data: bytes, side: int = 800) -> str:
    import base64
    from io import BytesIO

    from PIL import Image
    img = Image.open(BytesIO(data)).convert("RGB")
    img.thumbnail((side, side))
    buf = BytesIO()
    img.save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


async def smart_placement(biz, photo: bytes, ref: bytes, ref_note: str, fal_key: str, model: str,
                          transport: httpx.AsyncBaseTransport | None = None) -> str:
    """«Умная схема»: арт-директор смотрит на фото бизнеса и референс вместе и пишет, как вписать фото в раскладку референса. Пустая строка при сбое."""
    if not fal_key:
        return ""
    user = (f"Business: {biz['kind']}. Reference idea: {ref_note or 'see the second image'}. "
            "The photo is real and must stay recognisable. Describe how to build the new banner.")
    try:
        body = {"model": model, "max_tokens": 1200, "image_urls": [_small_url(photo), _small_url(ref, 700)],
                "messages": [{"role": "system", "content": SMART_SYSTEM}, {"role": "user", "content": user}]}
        async with httpx.AsyncClient(timeout=90, transport=transport) as http:
            r = await http.post(FAL_CHAT_URL, json=body, headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"})
        if r.status_code != 200:
            return ""
        raw = r.json()["choices"][0]["message"]["content"] or ""
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        return " ".join(str(data.get("placement") or "").split())[:900]
    except (httpx.HTTPError, ValueError, KeyError, IndexError, OSError):
        return ""


async def art_direction(biz, variant: dict, index: int, others: list[str], logo: dict | None, has_screen: bool,
                        fal_key: str, model: str, transport: httpx.AsyncBaseTransport | None = None, ref_note: str = "",
                        ref_image: bytes | None = None, layout: tuple[str, str, str] | None = None) -> dict:
    """Сцена и решение про логотип для баннера под это объявление. Всегда возвращает результат (при сбое запасной).
    ref_note: приём из референса, по которому рисуется баннер: сцена строится вокруг него, а не вокруг общего сюжета.
    ref_image: сам референс картинкой: арт-директор смотрит на него и повторяет тип кадра (люди, рука, предмет), а не берёт «обязательный сюжет» по номеру объявления."""
    key = scene_key(index)
    label, brief = SCENES[key]
    fallback = {"concept": key, "label": label, "audience": "", "scene": brief, "logo": False, "logo_reason": "", "ai": False}
    if not fal_key:
        return fallback
    logo_line = ("Логотипа нет, поле logo всегда false." if logo is None else
                 f"Логотип есть: пропорции {logo.get('ratio', 2.5):.1f}:1, оценка читаемости {logo.get('score') or 'не проверялась'} из 5"
                 + (f", замечания: {logo['note']}" if logo.get("note") else "") + ".")
    from app.creative.angles import effective_description
    user = (f"Бизнес: {biz['kind']}, «{biz['name']}». Описание владельца: {effective_description(biz)[:500]}\n"
            + (f"Пожелание к картинкам: {biz['visual_hint']}\n" if _field(biz, "visual_hint") else "")
            + f"Заголовок объявления: {variant['title']}\n"
            + (f"Угол подачи: {variant.get('angle')}\n" if variant.get("angle") else "")
            + (f"Обязательная композиция: {layout[1]} — {layout[2]}. Придумай героя кадра и сцену под эту композицию, показывая то, что бизнес правда предлагает "
               "(люди только если это уместно для услуги).\n" if layout else
               f"Обязательный сюжет: {label} — {brief}\n" if ref_image is None else
               "Сюжет определяет приложенный референс: внимательно посмотри на него и повтори тип кадра (есть ли в нём люди, рука, предмет, интерьер, сколько героев). "
               "Если в референсе нет людей, людей в сцене быть не должно; если героя один предмет, рисуй один предмет в той же манере. Свой сюжет не добавляй.\n")
            + (f"Подача по референсу: {ref_note}. Это главное: герой кадра, композиция и настроение берутся из этого приёма, а сюжет выше подстраивается "
               "под него и ему не противоречит (если в приёме главный предмет, а не люди, рисуй предмет и не сажай за столик людей).\n" if ref_note else "")
            + "Раскладку (где будет текст) не задавай: это решит художник. В сцене оставь спокойную область, на которую можно положить крупный текст.\n"
            + ("Настоящий скриншот есть: телефон с экраном обязателен. Человек держит его в руках ВЕРТИКАЛЬНО (портретная ориентация), экраном к камере, "
               "крупно (экран не меньше 20% высоты кадра). Телефон не лежит на столе и не держится горизонтально.\n" if has_screen
               else "Скриншота приложения нет. Телефон в кадр не добавляй, если сам бизнес не онлайн-сервис или приложение (кофейня, салон, магазин и другие офлайн-бизнесы показывай без телефона); интерфейс не придумывай.\n")
            + (f"Сцены других баннеров (не повторяй): {' | '.join(others)}\n" if others else "")
            + logo_line)
    body = {"model": model, "max_tokens": 1500, "messages": [{"role": "system", "content": ART_SYSTEM}, {"role": "user", "content": user}]}
    if ref_image is not None:   # референс картинкой (ту же схему «image_urls» использует чтение надписей на баннере)
        import base64
        body["image_urls"] = ["data:image/jpeg;base64," + base64.b64encode(ref_image).decode()]
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
    "Снижай оценку за: явно уродливые руки, лица и пальцы (лишние, сросшиеся или отсутствующие пальцы, оплавленное или перекошенное лицо; "
    "мелкие неточности, пальцы и части лица, закрытые руками или обрезанные кадром, зубы в улыбке и фото живого человека в кадре ошибкой не считай), "
    "лишние предметы, не соответствующие описанию бизнеса, "
    "нечитаемый текст (мелкая строка-подпись внизу с названием бизнеса и видом деятельности допустима и нужна, даже если повторяет слова заголовка, "
    "её не считай ошибкой), текст налезает на объект, "
    "а также если по баннеру нельзя понять, что именно рекламируется (какая услуга или товар) и кто рекламирует: это должны назвать заголовок, "
    "подпись или картинка (такие баннеры оценивай не выше 2), театральное удивление или шок на лице (широко раскрытые глаза и рот), показной "
    "восторг без повода, стоковая метафора вместо услуги (цепь, шестерёнки, светящийся мозг, рукопожатие над ноутбуком; такие баннеры оценивай не выше 2), шаблонный вид «сделано нейросетью» (лишний блеск, золотая пыль, рамки с орнаментом), "
    "нарисованный нейросетью логотип, эмблема, значок или фирменный знак, придуманный интерфейс приложения на экране телефона "
    "(такие баннеры оценивай не выше 2; исключения: экран заведомо не читается, либо в задании сказано, что на экране настоящий скриншот владельца: "
    "тогда интерфейс на экране не оценивай вообще), "
    "пустой или перегруженный кадр, логотип или зелёный/пурпурный прямоугольник-заглушка, оставшийся не закрашенным. "
    "В issues коротко по-русски перечисли реальные проблемы (не больше трёх). Если проблем нет, issues пустой."
)


async def review_banner(image_url: str, brief: str, fal_key: str, model: str,
                        transport: httpx.AsyncBaseTransport | None = None) -> dict | None:
    """Самопроверка готового баннера моделью со зрением. None, если оценить не удалось (тогда баннер не отбраковываем)."""
    body = {"model": model, "max_tokens": 800, "image_urls": [image_url],
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
