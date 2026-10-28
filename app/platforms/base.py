"""Что движок умеет просить у рекламной площадки.

Движок знает только этот интерфейс. Директ, VK Реклама и Авито подключаются как
отдельные реализации; пока есть только симулятор («пример данных»).
"""
from typing import Protocol


class Platform(Protocol):
    def set_state(self, unit_id: int, state: str, day: str) -> None:
        """state: 'active' или 'paused'."""

    def set_budget(self, unit_id: int, budget_micro: int, day: str) -> None:
        """Недельный бюджет объявления в микрорублях."""

    def create_variant(self, unit_id: int, day: str) -> int:
        """Создать второй вариант объявления для A/B-теста, вернуть его номер."""

    def touch(self, unit_id: int, day: str) -> None:
        """Отметить, что с объявлением что-то делали (чтобы оно «остыло»), ничего не меняя на площадке."""
