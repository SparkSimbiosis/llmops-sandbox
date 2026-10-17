"""Ціни курсу за 1k токенів (W2). Ціна залежить від моделі: strong дорожча за mini приблизно в 16.7 раза.

Модель без ціни -> cost_usd = NULL (а не 0): «нуль» означав би безкоштовний запит, а це брехня.
0 буде лише на cache-hit (W3). Прайс — живі дані: при переході на реальний ключ ціни моделей
додаються сюди.
"""

from decimal import ROUND_HALF_UP, Decimal

from sandbox.llm.types import Usage

PRICES_PER_1K: dict[str, tuple[Decimal, Decimal]] = {   # (вхід, вихід) USD за 1000 токенів
    "mock": (Decimal("0.00015"), Decimal("0.00060")),
    "mock-mini": (Decimal("0.00015"), Decimal("0.00060")),
    "mock-strong": (Decimal("0.00250"), Decimal("0.01000")),
    # DeepSeek: офіційні ціни за 1M токенів (вхід без кешу / вихід) — deepseek-chat $0.27 / $1.10,
    # deepseek-reasoner $0.55 / $2.19; тут — за 1k. Прайс — живі дані: звіряти з api-docs.deepseek.com.
    "deepseek-chat": (Decimal("0.00027"), Decimal("0.00110")),
    "deepseek-reasoner": (Decimal("0.00055"), Decimal("0.00219")),
}

_Q = Decimal("0.000001")  # NUMERIC(10, 6) у requests.cost_usd


def cost_usd(model: str, usage: Usage) -> Decimal | None:
    price = PRICES_PER_1K.get(model)
    if price is None:
        return None
    p_in, p_out = price
    raw = Decimal(usage.prompt_tokens) / 1000 * p_in + Decimal(usage.completion_tokens) / 1000 * p_out
    return raw.quantize(_Q, rounding=ROUND_HALF_UP)
