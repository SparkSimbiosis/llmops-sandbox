"""Маршрутизація dev-агента за складністю задачі (hw2): той самий механізм уроку W2, що й курсовий
роутер, але над власними аліасами fake-* і без жодного імпорту з курсу.

- plan — службовий виклик, завжди на дешевому класі (fast -> fake-mini);
- propose — за складністю: simple -> fast (fake-mini), complex -> balanced (fake-strong);
- складність дає planner (поле complexity у JSON-плані), а задача довша за 300 символів — завжди complex.

Таблиця живе в studio/routing.yaml: класи моделей (fast / balanced / reasoning) і правила зверху вниз.
Конвеєр і конструктор промптів (builder) читають ту саму таблицю, тож рішення в UI = рішення конвеєра.
"""

import pathlib

import yaml

from sandbox.agent.pricing import PRICES_PER_1K

ROUTING_FILE = pathlib.Path(__file__).resolve().parent / "studio" / "routing.yaml"
TABLE = yaml.safe_load(ROUTING_FILE.read_text(encoding="utf-8"))

SIMPLE = "simple"
COMPLEX = "complex"

CLASSES: dict[str, dict] = TABLE["classes"]
RULES: list[dict] = TABLE["rules"]


def alias(model_class: str, mode: str = "test") -> str:
    return CLASSES[model_class][mode]


CHEAP_MODEL = alias(TABLE["steps"]["plan"])
STRONG_MODEL = alias("balanced")

# довга задача майже напевно зачіпає кілька місць сайту: не віддаємо її дешевій моделі
LONG_TASK_CHARS = int(TABLE["long_task_chars"])

MODEL_BY_COMPLEXITY = {SIMPLE: alias("fast"), COMPLEX: alias("balanced")}

# recorded in hw2, executed in hw4: порядок fallback записано поруч із роутером, а крутитиме його
# конвеєр (reliability.run_chain) у hw4. Ланцюг починається з основної моделі й не повторює аліас.
AGENT_FALLBACK_CHAINS = {
    alias(cls): tuple(dict.fromkeys(alias(c) for c in TABLE["fallback"][cls]))
    for cls in ("fast", "balanced")
}


def task_complexity(task_text: str, planned: object) -> str:
    """Складність задачі: правило довжини сильніше за план; невідоме значення від planner -> simple
    (як «unknown» у таблиці намірів: незрозуміла задача однаково закінчиться needs_clarification)."""
    if len(task_text) > LONG_TASK_CHARS:
        return COMPLEX
    return planned if planned in MODEL_BY_COMPLEXITY else SIMPLE


def models(env_mode: str) -> list[dict]:
    """Доступні моделі стенда з routing.yaml: аліас, бік (test/real), класи, ціна за 1M токенів.
    test — завжди; real — лише якщо real увімкнено в оточенні сервісу (R-1)."""
    out = {}
    for cls, c in CLASSES.items():
        for side in ("test", "real"):
            m = out.setdefault(c[side], {"model": c[side], "side": side, "classes": [],
                                         "available": side == "test" or env_mode == "real"})
            m["classes"].append(cls)
    for m in out.values():
        p = PRICES_PER_1K.get(m["model"])
        m["usd_per_1m"] = None if p is None else {"input": float(p[0] * 1000), "output": float(p[1] * 1000)}
    return list(out.values())


def allowed_models(mode: str) -> set[str]:
    """Моделі, які можна обрати вручну для задачі в цьому режимі: mock -> test-аліаси, real -> real-аліаси."""
    side = "real" if mode == "real" else "test"
    return {c[side] for c in CLASSES.values()}


def route_task(step: str, complexity: str | None = None, mode: str = "mock") -> str:
    """Аліас моделі для кроку конвеєра. mode="real" (R-1) — той самий клас, але real-аліас з routing.yaml."""
    if mode == "real":
        cls = TABLE["steps"][step] if step in TABLE["steps"] else ("balanced" if complexity == COMPLEX else "fast")
        return alias(cls, "real")
    if step in TABLE["steps"]:
        return CHEAP_MODEL
    return MODEL_BY_COMPLEXITY.get(complexity, CHEAP_MODEL)


def decide(complexity: str | None, task_chars: int = 0, risk_flags=(), mode: str = "test") -> dict:
    """Рішення за правилами routing.yaml з поясненням (схема аудиту вибору моделі): клас, аліас, причина."""
    facts = {"high_risk": bool(risk_flags), "long_task": task_chars > LONG_TASK_CHARS,
             "complex": complexity == COMPLEX, "default": True}
    rule = next(r for r in RULES if facts.get(r["when"]))
    return {"chosen_model_class": rule["class"], "resolved_model": alias(rule["class"], mode),
            "rule": rule["when"], "reason": rule["reason"], "mode": mode}
