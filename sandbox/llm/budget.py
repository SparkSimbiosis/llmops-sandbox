"""Бюджет на реальну модель (опційно, hw2): денний ліміт REAL_BUDGET_USD.

- до 80% — працюємо як налаштовано;
- від 80% — попередження в лог і перемикання на дешевшу реальну модель (reasoner -> chat);
- від 100% — реальний маршрут вимикається, «все інше» йде в дешевий mock-mini.
Гроші — реальні, тож ліміт перевіряється на кожному запиті, а не раз на день.
"""

import logging

log = logging.getLogger("sandbox.budget")

WARN_SHARE = 0.8
CHEAPER = {"deepseek-reasoner": "deepseek-chat"}


def decide(model: str, spent_usd: float, budget_usd: float, fallback: str) -> str:
    if budget_usd <= 0 or spent_usd >= budget_usd:
        log.warning("budget exhausted spent=%.4f budget=%.2f -> %s", spent_usd, budget_usd, fallback)
        return fallback
    if spent_usd >= WARN_SHARE * budget_usd:
        cheaper = CHEAPER.get(model, model)
        log.warning("budget 80%% reached spent=%.4f budget=%.2f model %s -> %s", spent_usd, budget_usd, model, cheaper)
        return cheaper
    return model
