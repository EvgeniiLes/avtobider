"""Рекомендация по недельному бюджету для конкретного бизнеса.

Считаем по цифрам самого владельца (цена клиента или средний чек, доля обращений, ставшая клиентами) и по правилам Яндекса и сервиса:
- ниже минимума Директа (300 ₽ на кампанию) запустить нельзя;
- чтобы в кампанию вошли три объявления для сравнения, кампании нужно от 3 000 ₽ в неделю;
- Яндекс начинает подбирать ставки под обращения, когда их набирается 10 в неделю (справка Директа), значит, на неделю нужно
  10 цен обращения: именно столько, сколько владелец готов платить за обращение.
Реальную цену клика и долю обращений мы узнаем только после запуска, поэтому это оценка, и сервис прямо так и пишет.
"""
import math

from app.creative.compose import _price_in
from app.direct.launch import MIN_WEEKLY, RESERVE, START_SHARE, THREE_ADS_POOL
from app.engine.config import EngineConfig
from app.engine.cycle import target_lead_micro
from app.engine.util import EngineError, MICRO, rub

SHARE = (1 - RESERVE) * START_SHARE   # какую часть бюджета недели получает кампания на старте: остальное резерв и запас для роста


def _up(value: float) -> int:
    """Красивое число вверх: до 5 000 ₽ с шагом 100, дальше с шагом 500."""
    step = 500 if value >= 5000 else 100
    return int(math.ceil(value / step) * step)


def _money(rubles: float) -> str:
    return rub(rubles * MICRO)


def _num(value: float) -> str:
    """Число для человека: запятая вместо точки («4,5»)."""
    return f"{value:g}".replace(".", ",")


def budget_advice(biz, description: str = "") -> dict:
    """biz: строка бизнеса или словарь с weekly_budget_micro, avg_check_micro, target_cpa_micro, client_share.
    description: описание бизнеса; если цену клиента и чек не указали, а в описании названа одна цена, считаем от неё."""
    current = (biz["weekly_budget_micro"] or 0) / MICRO
    minimum = _up(MIN_WEEKLY / MICRO / SHARE)
    compare = _up(THREE_ADS_POOL / SHARE)
    leads_needed = EngineConfig().switch_leads
    assumed = None
    if not (biz["avg_check_micro"] or biz["target_cpa_micro"]):
        prices = sorted({amount for amount, _ in _price_in(description)})
        if len(prices) == 1:   # цена в описании одна: берём её за средний чек, но говорим об этом
            assumed = prices[0]
            biz = {**dict(biz), "avg_check_micro": assumed * MICRO}
    try:
        target_lead = target_lead_micro(biz) / MICRO
    except EngineError:
        target_lead = 0
    out = {"current": round(current), "minimum": minimum, "compare": compare, "recommended": None, "target_lead": round(target_lead) if target_lead else None,
           "leads_now": None, "assumed_check": assumed, "leads_needed": leads_needed}
    details: list[str] = []
    if target_lead > 0:
        need_pool = leads_needed * target_lead
        out["recommended"] = max(compare, _up(need_pool / SHARE))
        out["leads_now"] = round(current * SHARE / target_lead, 1) if current else 0
        details.append(
            f"Яндексу нужно {leads_needed} обращений в неделю, чтобы подбирать ставки под обращения, а не под клики. При цене обращения не выше вашей цели "
            f"({_money(target_lead)}) это {_money(need_pool)} в неделю. Кампания получает {round(SHARE * 100)}% бюджета на старте, остальное резерв и запас для роста, "
            f"поэтому бюджет нужен больше: {_money(out['recommended'])}."
            + (f" Цену клиента вы не указали, считаем от цены из описания ({_money(assumed)}) как от среднего чека." if assumed else ""))
    else:
        prices = sorted({amount for amount, _ in _price_in(description)})
        details.append("Укажите средний чек или сколько готовы платить за клиента, и мы посчитаем бюджет под 10 обращений в неделю."
                       + (f" В описании названы цены: {', '.join(_money(p) for p in prices[:4])}. Какая из них у вас типичная?" if len(prices) > 1 else ""))

    rec = out["recommended"]
    if current <= 0:
        level, summary = "none", f"Укажите бюджет. Запустить рекламу можно от {_money(minimum)} в неделю" + (f", а рекомендуем около {_money(rec)}." if rec else f"; чтобы сравнивать три объявления, нужно от {_money(compare)}.")
    elif current < minimum:
        level, summary = "blocked", f"Это слишком мало: Директ требует от {_money(MIN_WEEKLY / MICRO)} в неделю на кампанию, а кампании достаётся {round(SHARE * 100)}% бюджета. Нужно хотя бы {_money(minimum)}."
    elif current < compare:
        level = "tiny"
        summary = (f"Запустить можно, но на {_money(current)} в кампанию войдёт одно-два объявления и на каждое придётся мало кликов. "
                   f"Чтобы сравнивать три объявления, нужно от {_money(compare)}" + (f", а рекомендуем около {_money(rec)}." if rec and rec > compare else "."))
    elif rec and current < rec:
        level = "low"
        summary = (f"Рекомендуем около {_money(rec)} в неделю. На {_money(current)} выйдет примерно {_num(out['leads_now'])} обращений в неделю при цене обращения {_money(target_lead)}, "
                   f"меньше {leads_needed}: сервис будет платить за клики и не сразу перейдёт на подбор ставок под обращения.")
    else:
        level = "ok"
        summary = (f"Бюджета достаточно: на {_money(current)} выйдет около {_num(out['leads_now'])} обращений в неделю при цене обращения {_money(target_lead)}." if rec
                   else f"Для сравнения трёх объявлений бюджета достаточно ({_money(current)} при минимуме {_money(compare)}).")
    details.append("Это оценка по вашим цифрам: настоящую цену клика и долю обращений мы увидим через 1–2 недели после запуска, тогда стоит пересчитать.")
    out.update({"level": level, "summary": summary, "details": details})
    return out
