"""Маршрутизація (W2): сервіс сам обирає модель під тип питання.

Найбільший важіль вартості — дороге дістається лише складному: прохання повернути гроші, скарги,
«терміново» йдуть на сильну модель, решта — на дешеву. Маркери — українською й англійською.

Повернення — саме прохання про дію («поверніть гроші», «хочу повернути», «refund my order»), а не корінь
«поверн»: «Які умови повернення товару?» чи «Як повернутися на головну?» — довідка, сильна модель їй
не потрібна. Неоднозначне лишається на дешевій моделі.
"""

import re

STRONG_MARKERS = ("терміново", "скарг", "urgent", "complaint")
REFUND_ACTION = re.compile(
    r"\bповерн(іть|и)\b"                                                    # поверніть / поверни гроші
    r"|\b(хочу|хотів би|хотіла б|прошу|треба|потрібно|мушу)\s+(\w+\s+)?повернути\b"  # хочу повернути
    r"|\b(оформ|зроб)\w*\s+повернення\b"                                    # оформіть повернення
    r"|\brefund\s+(my|me|the|this)\b|\brefund\W+please\b|\bplease\W+refund\b"
    r"|\b(want|need|like|get|request|issue|give me)\s+(a\s+)?refund\b"
    r"|\bmoney back\b",
    re.I)
# (опц. hw2) третій маршрут: типові FAQ (пароль, вхід, замовлення) — дешевий mock-mini з готовими відповідями;
# ескалації — mock-strong (там tool_call create_ticket і HITL); «все інше» — реальна модель, якщо вона ввімкнена.
FAQ_MARKERS = ("пароль", "password", "вхід", "login", "замовлен", "order", "#")
TIERS = {"mock": {"mini": "mock-mini", "strong": "mock-strong"}}
REAL_MODELS = ("deepseek-chat", "deepseek-reasoner")


def route(message: str, default_model: str, real_model: str = "") -> str:
    text = message.lower()
    if default_model not in TIERS:
        return default_model             # на реальному ключі в MODEL маршрут вироджується в одну модель
    if any(m in text for m in STRONG_MARKERS) or REFUND_ACTION.search(text):
        return TIERS[default_model]["strong"]
    if real_model and not any(m in text for m in FAQ_MARKERS):
        return real_model                # третій маршрут: загальні питання — справжня модель
    return TIERS[default_model]["mini"]


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
    # реальні моделі пісочниці: fallback між двома моделями DeepSeek
    "deepseek-chat": ["deepseek-chat", "deepseek-reasoner"],
    "deepseek-reasoner": ["deepseek-reasoner", "deepseek-chat"],
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
