"""Простые русские названия для значений, которые отдаёт Директ."""

TYPES = {
    "TEXT_CAMPAIGN": "Текстово-графические объявления",
    "UNIFIED_CAMPAIGN": "Единая перфоманс-кампания",
    "MOBILE_APP_CAMPAIGN": "Реклама мобильного приложения",
    "DYNAMIC_TEXT_CAMPAIGN": "Динамические объявления",
    "CPM_BANNER_CAMPAIGN": "Медийный баннер",
    "SMART_CAMPAIGN": "Смарт-баннеры",
}

STATES = {
    "ON": "Показы идут",
    "OFF": "Остановлена",
    "SUSPENDED": "Приостановлена",
    "ENDED": "Закончилась",
    "ARCHIVED": "В архиве",
    "CONVERTED": "Перенесена",
}

STATUSES = {
    "DRAFT": "Черновик",
    "MODERATION": "На проверке",
    "ACCEPTED": "Проверка пройдена",
    "REJECTED": "Отклонена при проверке",
}


def describe_campaign(raw: dict) -> dict:
    """Кампания из ответа Директа в виде, понятном человеку."""
    return {
        "id": raw.get("Id"),
        "name": raw.get("Name", ""),
        "type": TYPES.get(raw.get("Type"), raw.get("Type", "")),
        "state": STATES.get(raw.get("State"), raw.get("State", "")),
        "status": STATUSES.get(raw.get("Status"), raw.get("Status", "")),
        "currency": raw.get("Currency", ""),
        "start_date": raw.get("StartDate", ""),
    }
