"""Мелкие помощники: даты и деньги."""
from datetime import date, timedelta

MICRO = 1_000_000


def day_add(day: str, n: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def days_between(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def rub(micro: float) -> str:
    """Сумма для показа человеку: «1 234 ₽» (неразрывные пробелы)."""
    return f"{round(micro / MICRO):,}".replace(",", " ") + " ₽"


def plural(n: int, one: str, few: str, many: str) -> str:
    """Число со словом в правильной форме: 1 обращение, 2 обращения, 5 обращений."""
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        word = one
    elif 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"


class EngineError(Exception):
    """Понятная пользователю причина, по которой действие не выполнено."""

    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.message = message
        self.code = code
