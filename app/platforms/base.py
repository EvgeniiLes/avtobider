"""Что движок умеет просить у рекламной площадки.

Движок знает только этот интерфейс. Директ, VK Реклама и Авито подключаются как
отдельные реализации; пока есть только симулятор («пример данных»).

Площадки бывают двух видов. Обычная: у каждого объявления свой бюджет (set_budget), сравнение двух
вариантов запускается отдельным объявлением (create_variant). С общим бюджетом (shared_budget = True, так
устроен Директ): объявления лежат в одной кампании и делят её недельный лимит; сервис меняет лимит и стратегию
всей кампании (set_campaign_budget, set_strategy), а объявление может только остановить или запустить.
"""
from typing import Protocol


class Platform(Protocol):
    shared_budget: bool

    def set_state(self, unit_id: int, state: str, day: str) -> None:
        """state: 'active' или 'paused'."""

    def set_budget(self, unit_id: int, budget_micro: int, day: str) -> None:
        """Недельный бюджет объявления в микрорублях (площадки без общего бюджета)."""

    def create_variant(self, unit_id: int, day: str) -> int:
        """Создать второй вариант объявления для A/B-теста, вернуть его номер (площадки без общего бюджета)."""

    def touch(self, unit_id: int, day: str) -> None:
        """Отметить, что с объявлением что-то делали (чтобы оно «остыло»), ничего не меняя на площадке."""

    def set_campaign_budget(self, campaign_id: int, weekly_micro: int, day: str) -> None:
        """Недельный лимит всей кампании (площадки с общим бюджетом). campaign_id: номер в нашей таблице campaigns."""

    def set_strategy(self, campaign_id: int, strategy: str, day: str) -> None:
        """Стратегия кампании: 'clicks' или 'conversions' (площадки с общим бюджетом)."""
