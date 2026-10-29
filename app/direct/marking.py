"""Данные рекламодателя для маркировки: проверка ИНН и соответствие видов плательщика значениям API Директа.

ИНН у нас не хранится: он передаётся в кабинет Директа (Clients.update) и сразу забывается.
"""
import re

# что выбирает владелец -> TinType в API Директа и длина ИНН
KINDS = {
    "person": ("PHYSICAL", 12, "Физическое лицо или самозанятый"),
    "ip": ("INDIVIDUAL", 12, "Индивидуальный предприниматель"),
    "org": ("LEGAL", 10, "Организация (ООО и другие)"),
}


def valid_inn(inn: str, length: int) -> bool:
    """ИНН: только цифры нужной длины и верные контрольные цифры (так отсеиваются опечатки)."""
    if not re.fullmatch(r"\d+", inn or "") or len(inn) != length:
        return False
    d = [int(c) for c in inn]
    if length == 10:
        return d[9] == sum(w * x for w, x in zip((2, 4, 10, 3, 5, 9, 4, 6, 8), d)) % 11 % 10
    c1 = sum(w * x for w, x in zip((7, 2, 4, 10, 3, 5, 9, 4, 6, 8), d)) % 11 % 10
    c2 = sum(w * x for w, x in zip((3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8), d)) % 11 % 10
    return d[10] == c1 and d[11] == c2


def is_filled(data: dict) -> bool:
    """Данные рекламодателя внесены: указаны вид плательщика, ИНН и название или имя."""
    return bool(data.get("tin_type") and data.get("has_tin") and data.get("org_name"))
