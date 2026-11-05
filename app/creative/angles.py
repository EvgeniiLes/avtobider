"""Углы подачи рекламы: о чём объявление. Список собран из исследования ниши (docs/ad-hooks.md).

Угол «с фактом» (цена, акция, срок, безопасность…) доступен только если владелец сам назвал такой факт в описании бизнеса.
Так сервис никогда не обещает того, чего владелец не говорил.
"""
import re

# id: (название, суть для модели, регулярка факта в описании или None, если факт не нужен)
ANGLES: dict[str, tuple[str, str, str | None]] = {
    "booking": ("Простая запись", "записаться легко и быстро (онлайн, по телефону, в мессенджере), без лишних хлопот", None),
    "choice": ("Помощь с выбором", "поможем подобрать и выбрать, если сомневаетесь", None),
    "identity": ("Стиль и образ", "подчеркнёт ваш стиль и индивидуальность, об образе и ощущении, а не об услуге", None),
    "comfort": ("Комфорт и уход", "спокойная атмосфера, бережный уход, можно отдохнуть", None),
    "location": ("Расположение", "удобный адрес: рядом с метро, домом, в центре, парковка",
                 r"метро|адрес|рядом|район|улиц|ул\.|проспект|центр|парков"),
    "price": ("Цена", "назвать конкретную цену услуги дословно из описания", r"\d[\d\s ]*\s?(?:₽|руб|р\.)"),
    "promo": ("Акция", "акция или бонус для новых клиентов, дословно из описания", r"скидк|акци|подар|комплимент|первый визит|первом визите|бонус"),
    "durability": ("Долговечность", "результат держится долго, срок называть только если он есть в описании", r"держит|без сколов|недел|носится"),
    "safety": ("Безопасность", "стерильность и чистота: чем обрабатываются инструменты, одноразовые материалы", r"стерил|крафт|одноразов|дезинф|безопасн"),
    "trust": ("Опыт и мастера", "опыт, мастера, рейтинг, отзывы, дословно из описания", r"опыт|лет\b|мастер|рейтинг|отзыв|сертиф|с 20\d\d"),
    "kit": ("Комплекс услуг", "несколько услуг за один визит", r"маникюр.*педикюр|педикюр.*маникюр|комплекс"),
    "event": ("Повод", "подготовка к событию: свадьба, выпускной, праздник", r"свадеб|выпускн|новогод|праздник|корпоратив"),
    "urgency": ("Свободные окна", "ограниченные места или срок, только если это названо в описании", r"окн|мест\b|мест[аы]\b|только до|на этой неделе|до \d"),
}

ONLINE = re.compile(r"(?i)онлайн|интернет|приложени|платформ|подписк|игр[аыуе]\b|телеграм|софт|сайт")
OFFLINE = re.compile(r"(?i)салон|студи|клиник|кабинет|барбер|маникюр|педикюр|массаж|стоматолог|косметолог|парикмахер|ремонт|магазин|кафе|ресторан|фитнес|зал\b|мастерск|центр\b")
APPOINTMENT = re.compile(r"(?i)салон|студи|клиник|кабинет|барбер|маникюр|педикюр|массаж|стоматолог|косметолог|парикмахер|врач")

CTA_SENTENCES = {
    "Записаться": "Запишитесь онлайн",
    "Играть": "Присоединяйтесь к игре",
    "Заказать": "Оформите заказ на сайте",
    "Попробовать": "Попробуйте сервис",
    "Узнать подробнее": "Подробности на сайте",
}


def profile(biz) -> dict:
    """Какой это бизнес: онлайн или офлайн, какое действие предлагаем клиенту. От этого зависят кнопка и шаблонные тексты."""
    def field(key: str) -> str:
        try:
            return biz[key] or ""
        except (KeyError, IndexError):
            return ""

    kind, description = field("kind"), field("description")
    online = bool(ONLINE.search(kind)) and not OFFLINE.search(kind)
    local = bool(not online and (field("maps_url") or available_location(description) or OFFLINE.search(kind)))
    if APPOINTMENT.search(kind) and not online:
        cta = "Записаться"
    elif online and re.search(r"(?i)игр", kind):
        cta = "Играть"
    elif re.search(r"(?i)магазин|купить|доставк|товар", kind + " " + description[:200]):
        cta = "Заказать"
    elif online:
        cta = "Попробовать"
    else:
        cta = "Узнать подробнее"
    return {"online": online, "local": local, "cta": cta, "cta_sentence": CTA_SENTENCES[cta], "appointment": cta == "Записаться"}


def is_nationwide(biz) -> bool:
    """Владелец выбрал «показывать по всей России»: онлайн-услуга без города. Тогда регион показа вся Россия, а город в рекламе не упоминаем."""
    try:
        return bool(biz["nationwide"])
    except (KeyError, IndexError):
        return False


MAX_DISTRICTS = 6


def split_districts(text: str) -> list[str]:
    """Районы из строки владельца: через запятую, точку с запятой или с новой строки; без повторов и пустых."""
    found: list[str] = []
    for part in re.split(r"[,;\n]+", text or ""):
        name = re.sub(r"\s+", " ", part).strip(" .")[:60]
        if name and name.lower() not in {x.lower() for x in found}:
            found.append(name)
    return found[:MAX_DISTRICTS]


def join_districts(text: str) -> str:
    return ", ".join(split_districts(text))


def districts_of(biz) -> list[str]:
    """Районы показа. По всей России районов нет, даже если владелец записал их раньше."""
    if is_nationwide(biz):
        return []
    try:
        return split_districts(biz["districts"])
    except (KeyError, IndexError):
        return []


def needs_districts(biz) -> bool:
    """Рекламе нужен район: бизнес принимает клиентов на месте, а не работает онлайн по всей России."""
    return not is_nationwide(biz) and not profile(biz)["online"]


def where_text(biz) -> str:
    """Где показываем рекламу, словами для подсказок модели."""
    if is_nationwide(biz):
        return "вся Россия, онлайн: города в объявлении нет, про город и «рядом» не пиши"
    try:
        city = biz["city"] or "не указан"
    except (KeyError, IndexError):
        return "не указан"
    districts = districts_of(biz)
    return f"{city}, районы показа: {', '.join(districts)}" if districts else city


PROMO = re.compile(r"скидк|акци[июяе]|акционн|подар|комплимент|бонус|\d\s?%", re.I)


def effective_description(biz) -> str:
    """Описание, из которого делается реклама. Если владелец не разрешил скидки и акции, предложения с ними из описания убираются,
    чтобы ни тексты, ни картинки их не использовали."""
    try:
        text = (biz["description"] or "")
        allowed = bool(biz["allow_promo"])
    except (KeyError, IndexError):
        return (biz["description"] or "") if biz else ""
    if allowed:
        return text
    kept = [s for s in re.split(r"(?<=[.!?])\s+", text) if not PROMO.search(s)]
    return " ".join(kept).strip()


def available_location(description: str) -> bool:
    return "location" in available(description)


# Теги шаблонных вариантов (запасные тексты без ИИ)
TEMPLATE_ANGLES = ("location", "booking", "price", "booking", "choice", "location")


def label(angle: str) -> str:
    return ANGLES[angle][0] if angle in ANGLES else ""


def available(description: str) -> list[str]:
    """Углы, доступные для этого бизнеса: без фактов всегда, с фактами только если факт назван в описании."""
    text = (description or "").lower()
    return [a for a, (_n, _b, need) in ANGLES.items() if need is None or re.search(need, text)]


def business_block(biz) -> str:
    p = profile(biz)
    where = ("Это онлайн-проект: физического адреса нет, не пиши про «рядом», «у метро», «приходите», «запись к мастеру»."
             if p["online"] else "Это бизнес с адресом, клиенты приходят на место." if p["local"] else "")
    districts = districts_of(biz)
    if districts and needs_districts(biz):   # реклама идёт по районам: минимум два варианта прямо говорят, что мы рядом с клиентом
        where += (f" Реклама показывается людям в районах: {', '.join(districts)}. Минимум два варианта пиши про то, что мы рядом с клиентом "
                  "(угол location): назови район из списка и склоняй его по-русски: «Маникюр в Хамовниках», «Груминг в Адлере», «Рядом с вами на Арбате». Не пиши «в районе Адлер»: слово «район» перед названием не ставь.")
    return f"{where} Главное действие клиента на кнопке: «{p['cta']}». Пиши под это действие.".strip()


def prompt_block(description: str) -> str:
    """Список углов для задания модели: каждый вариант под свой угол."""
    lines = [f"- {a}: {ANGLES[a][0]}. {ANGLES[a][1]}" for a in available(description)]
    return "Доступные углы подачи (id: название, суть). Каждый вариант пиши под один угол, углы не повторяй:\n" + "\n".join(lines)
