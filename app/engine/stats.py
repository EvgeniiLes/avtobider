"""Вероятности для оценки объявлений. Библиотеки для статистики не нужны."""
import random

from app.engine.config import EngineConfig


def prob_ok(clicks: int, leads: int, spend_micro: int, target_lead_micro: int, rng: random.Random, samples: int) -> float:
    """Вероятность, что обращение обходится не дороже цели.

    Доля кликов, приводящих к обращению, неизвестна точно. Описываем её
    распределением Beta(1 + обращения, 1 + клики − обращения) и считаем, как часто
    она получается не меньше нужной: цена клика / допустимая цена обращения.
    """
    if clicks <= 0 or target_lead_micro <= 0:
        return 0.0
    needed = (spend_micro / clicks) / target_lead_micro
    if needed >= 1:
        return 0.0
    a, b = 1 + leads, 1 + clicks - leads
    hits = sum(1 for _ in range(samples) if rng.betavariate(a, b) >= needed)
    return hits / samples


def prob_b_better(a_clicks: int, a_leads: int, b_clicks: int, b_leads: int, rng: random.Random, samples: int) -> float:
    """Вероятность, что у варианта Б доля обращений выше, чем у варианта А."""
    a1, a2 = 1 + a_leads, 1 + max(a_clicks - a_leads, 0)
    b1, b2 = 1 + b_leads, 1 + max(b_clicks - b_leads, 0)
    wins = sum(1 for _ in range(samples) if rng.betavariate(b1, b2) > rng.betavariate(a1, a2))
    return wins / samples


def classify(p: float, cfg: EngineConfig) -> str:
    if p >= cfg.p_good:
        return "good"
    if p <= cfg.p_bad:
        return "bad"
    return "borderline"


def enough_data(clicks: int, spend_micro: int, target_cpa_micro: int, cfg: EngineConfig) -> bool:
    return clicks >= cfg.min_clicks or spend_micro >= cfg.min_spend_factor * target_cpa_micro
