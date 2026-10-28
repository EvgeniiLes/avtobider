import random

from app.engine.config import EngineConfig
from app.engine.stats import classify, enough_data, prob_b_better, prob_ok

CFG = EngineConfig()
RUB = 1_000_000


def rng():
    return random.Random(1)


def test_cheap_leads_are_good():
    # 200 кликов по 30 ₽ = 6000 ₽, 40 обращений: 150 ₽ за обращение при цели 300 ₽
    p = prob_ok(200, 40, 6000 * RUB, 300 * RUB, rng(), CFG.samples)
    assert p > 0.95
    assert classify(p, CFG) == "good"


def test_expensive_leads_are_bad():
    # 200 кликов, 3 обращения: 2000 ₽ за обращение при цели 300 ₽
    p = prob_ok(200, 3, 6000 * RUB, 300 * RUB, rng(), CFG.samples)
    assert p < 0.05
    assert classify(p, CFG) == "bad"


def test_near_the_target_is_borderline():
    # 100 кликов по 30 ₽, 10 обращений: 300 ₽ за обращение ровно на цели
    p = prob_ok(100, 10, 3000 * RUB, 300 * RUB, rng(), CFG.samples)
    assert 0.2 < p < 0.8
    assert classify(p, CFG) == "borderline"


def test_zero_leads_is_bad_with_enough_clicks():
    p = prob_ok(150, 0, 4500 * RUB, 300 * RUB, rng(), CFG.samples)
    assert p < 0.2


def test_more_data_means_more_certainty():
    few = prob_ok(30, 6, 900 * RUB, 300 * RUB, rng(), CFG.samples)
    many = prob_ok(300, 60, 9000 * RUB, 300 * RUB, rng(), CFG.samples)
    assert many > few


def test_impossible_target_and_empty_data():
    assert prob_ok(0, 0, 0, 300 * RUB, rng(), 100) == 0.0
    assert prob_ok(100, 5, 100_000 * RUB, 300 * RUB, rng(), 100) == 0.0  # клик дороже цели обращения
    assert prob_ok(100, 5, 3000 * RUB, 0, rng(), 100) == 0.0


def test_ab_comparison():
    clear = prob_b_better(200, 10, 200, 40, rng(), CFG.samples)
    assert clear > 0.99
    reverse = prob_b_better(200, 40, 200, 10, rng(), CFG.samples)
    assert reverse < 0.01
    same = prob_b_better(200, 20, 200, 20, rng(), CFG.samples)
    assert 0.3 < same < 0.7


def test_enough_data_rules():
    assert enough_data(30, 0, 800 * RUB, CFG)
    assert enough_data(5, 1600 * RUB, 800 * RUB, CFG)
    assert not enough_data(10, 500 * RUB, 800 * RUB, CFG)
