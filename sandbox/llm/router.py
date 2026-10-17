"""Маршрутизація (W2): сервіс сам обирає модель під тип питання.

Найбільший важіль вартості — дороге дістається лише складному: повернення, скарги, «терміново»
йдуть на сильну модель, решта — на дешеву. Маркери — українською й англійською.
"""

STRONG_MARKERS = ("поверн", "терміново", "скарг", "refund", "urgent", "complaint")
TIERS = {"mock": {"mini": "mock-mini", "strong": "mock-strong"}}


def route(message: str, default_model: str) -> str:
    tier = "strong" if any(m in message.lower() for m in STRONG_MARKERS) else "mini"
    # на реальному ключі (MODEL=gpt-5-mini тощо) маршрут вироджується в одну модель
    return TIERS[default_model][tier] if default_model in TIERS else default_model


# fallback chain: mock-strong -> mock-mini -> заглушка; mock-mini -> mock -> заглушка.
# Записано в hw2, код, який по ньому перемикається, — у hw4. Заглушка (текст деградації) —
# не елемент ланцюга, а те, що віддаємо після нього. Кожен ланцюг — щонайменше два різні аліаси
# з gateway/litellm-config.yaml.
FALLBACK_CHAINS: dict[str, list[str]] = {
    "mock-strong": ["mock-strong", "mock-mini"],
    "mock-mini": ["mock-mini", "mock"],
    "mock": ["mock", "mock-mini"],
    "gpt-5": ["gpt-5", "gpt-5-mini"],
    "gpt-5-mini": ["gpt-5-mini", "gpt-5"],
    "claude-sonnet": ["claude-sonnet", "claude-haiku"],
    "claude-haiku": ["claude-haiku", "claude-sonnet"],
}
# azure-gpt-5 свідомо не включено: у конфігу gateway це заглушка deployment-а


def providers_for(model: str) -> list[str]:
    """Порядок провайдерів для GET /providers (W5): MODEL=mock -> mock-strong, mock-mini, mock."""
    heads = list(TIERS[model].values())[::-1] if model in TIERS else [model]
    seen: list[str] = []
    for head in heads:
        for alias in FALLBACK_CHAINS[head]:
            if alias not in seen:
                seen.append(alias)
    return seen


def check_model(model: str) -> None:
    # тиха підміна одноелементним ланцюгом приховала б помилку конфігурації — краще впасти на старті
    if model not in TIERS and model not in FALLBACK_CHAINS:
        raise SystemExit(f"no fallback chain for MODEL={model}")
