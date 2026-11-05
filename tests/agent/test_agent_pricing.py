"""Ціни агента hw2 (окремий модуль від курсового): питома ціна рівно 0.000003 і 0.000050 за будь-якого
складу запиту, округлення до 6 знаків, неоцінений аліас -> None з WARN."""

import logging
from decimal import Decimal

import pytest

from sandbox.agent import pricing
from sandbox.agent.pricing import PRICES_PER_1K, cost_usd, total_cost

UNIT = {"fake-mini": Decimal("0.000003"), "fake-strong": Decimal("0.000050")}


@pytest.mark.parametrize("model", sorted(UNIT))
@pytest.mark.parametrize("pt, ct", [(1, 0), (0, 1), (19, 11), (120, 30), (412, 96), (1000, 1), (7, 993)])
def test_unit_price_is_exact_for_any_mix(model, pt, ct):
    c = cost_usd(model, pt, ct)
    assert c / (pt + ct) == UNIT[model]
    assert c.as_tuple().exponent == -6                           # NUMERIC(10, 6)


def test_strong_to_mini_ratio_and_in_equals_out():
    assert UNIT["fake-strong"] / UNIT["fake-mini"] == Decimal(50) / 3
    # синтетичні fake-ціни: вхід = вихід; DeepSeek (R-1) — справжній прайс, де вихід дорожчий
    assert all(PRICES_PER_1K[m][0] == PRICES_PER_1K[m][1] for m in ("fake-mini", "fake-strong"))
    assert all(PRICES_PER_1K[m][1] > PRICES_PER_1K[m][0] for m in pricing.REAL_MODELS)


def test_example_task_from_plan():
    # приклад 3.3: plan на fake-mini 120+30, propose на fake-strong 412+96
    plan, propose = cost_usd("fake-mini", 120, 30), cost_usd("fake-strong", 412, 96)
    assert (plan, propose) == (Decimal("0.000450"), Decimal("0.025400"))
    assert total_cost([plan, propose]) == Decimal("0.025850")


def test_rounding_half_up_to_six_places(monkeypatch):
    monkeypatch.setitem(PRICES_PER_1K, "fake-half", (Decimal("0.0005"), Decimal("0.0005")))
    assert cost_usd("fake-half", 1, 0) == Decimal("0.000001")     # 0.0000005 -> вгору
    assert cost_usd("fake-half", 3, 0) == Decimal("0.000002")     # 0.0000015 -> вгору
    assert cost_usd("fake-mini", 0, 0) == Decimal("0.000000")


def test_unpriced_alias_is_none_with_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="sandbox.agent"):
        assert cost_usd("fake-unknown", 10, 10) is None
        assert cost_usd("mock-mini", 10, 10) is None               # курсові аліаси агент не оцінює
    assert "agent unpriced model=fake-unknown" in caplog.text


def test_total_cost_null_rule():
    a, b = Decimal("0.000450"), Decimal("0.025400")
    assert total_cost([a, b]) == Decimal("0.025850")
    assert total_cost([a, None]) == a                             # як SQL SUM: NULL пропускається
    assert total_cost([None, None]) is None and total_cost([]) is None


def test_pricing_labels():
    assert pricing.PRICING == {"fake": "synthetic", "real": "deepseek_list_price"}
