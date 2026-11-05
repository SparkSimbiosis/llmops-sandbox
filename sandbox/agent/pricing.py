"""Ціни dev-агента (hw2). Окремий модуль від курсового sandbox/llm/pricing.py: агент курс не імпортує,
а курс — агента.

Долари синтетичні: fake-моделі нічого не коштують, ціни потрібні, щоб показати механізм —
вартість кожного виклику, підсумок задачі й GET /agent/cost. Принцип той самий, що в курсі:
ціна входу дорівнює ціні виходу, тому питома ціна за токен однакова на будь-якому рядку
(0.000003 і 0.000050), а fake-strong рівно в 16.67 раза дорожчий за fake-mini.
R-1: ціни реальних моделей DeepSeek — справжні долари (ті самі, що в курсовому sandbox/llm/pricing.py;
прайс — живі дані, звіряти з api-docs.deepseek.com у день прогону).

Правило cost_usd: > 0 — оцінена відповідь моделі; NULL — модель без ціни або відповіді не було.
"""

import logging
from decimal import ROUND_HALF_UP, Decimal

log = logging.getLogger("sandbox.agent")

# (вхід, вихід) у синтетичних доларах за 1000 токенів
PRICES_PER_1K: dict[str, tuple[Decimal, Decimal]] = {
    "fake-mini": (Decimal("0.003"), Decimal("0.003")),
    "fake-strong": (Decimal("0.050"), Decimal("0.050")),
    # DeepSeek за 1M токенів (вхід без кешу / вихід): chat $0.27 / $1.10, reasoner $0.55 / $2.19
    "deepseek-chat": (Decimal("0.00027"), Decimal("0.00110")),
    "deepseek-reasoner": (Decimal("0.00055"), Decimal("0.00219")),
}
REAL_MODELS = frozenset({"deepseek-chat", "deepseek-reasoner"})

# що означають долари у /agent/cost: fake — синтетика, real — реальні ціни (з hw4)
PRICING = {"fake": "synthetic", "real": "deepseek_list_price"}

_Q = Decimal("0.000001")  # NUMERIC(10, 6) у agent_calls.cost_usd і agent_tasks.total_cost_usd
ZERO = Decimal("0").quantize(_Q)   # W3: cache-hit — відповідь без виклику моделі коштує рівно 0


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> Decimal | None:
    p = PRICES_PER_1K.get(model)
    if p is None:
        log.warning("agent unpriced model=%s", model)
        return None
    return ((Decimal(prompt_tokens) * p[0] + Decimal(completion_tokens) * p[1]) / 1000).quantize(_Q, ROUND_HALF_UP)


def total_cost(costs) -> Decimal | None:
    """Підсумок задачі за правилом SQL SUM: NULL-виклики пропускаються, а якщо оціненого
    виклику немає жодного — підсумок теж NULL (а не 0, бо 0 означав би безкоштовну відповідь)."""
    priced = [c for c in costs if c is not None]
    return sum(priced, Decimal("0")).quantize(_Q) if priced else None


def estimate_usd(model: str, messages: list[dict], max_tokens: int) -> Decimal | None:
    """Верхня оцінка ціни real-виклику до відправки: ~1 токен на 3 символи входу + max_tokens виходу.
    Бюджет-гард порівнює її із залишком: вартість вже відповідних викликів знаємо точно, цього — ні."""
    p = PRICES_PER_1K.get(model)
    if p is None:
        return None
    prompt_tokens = sum(len(str(m.get("content") or "")) for m in messages) // 3 + 1
    return ((Decimal(prompt_tokens) * p[0] + Decimal(max_tokens) * p[1]) / 1000).quantize(_Q, ROUND_HALF_UP)
