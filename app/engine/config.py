"""Пороги правил. Начальные значения описаны в docs/algorithms.md."""
from dataclasses import dataclass


@dataclass(frozen=True)
class EngineConfig:
    window_days: int = 14          # за сколько дней смотрим статистику
    lag_days: int = 1              # последние сутки не берём: данные ещё не дошли
    min_clicks: int = 30           # меньше кликов и расхода: «ещё собираем данные»
    min_spend_factor: float = 2.0  # или расход не меньше N цен клиента
    p_good: float = 0.80           # вероятность «окупается» для хорошего объявления
    p_bad: float = 0.20            # и для слабого
    raise_step: float = 0.20       # на сколько повышаем бюджет за шаг
    cooldown_days: int = 2         # после изменения объявление не трогаем
    reserve: float = 0.10          # часть недельного бюджета, которую не раздаём
    block_growth_at: float = 0.90  # при таком расходе недели рост запрещён
    ab_min_clicks: int = 100       # кликов на каждый вариант для вердикта
    ab_max_days: int = 14          # тест не длиннее
    ab_confidence: float = 0.95    # уверенность для победителя
    samples: int = 4000            # размер выборки для расчёта вероятностей
