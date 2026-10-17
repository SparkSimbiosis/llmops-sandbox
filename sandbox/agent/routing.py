"""Маршрутизація dev-агента за складністю задачі (hw2): той самий механізм уроку W2, що й курсовий
роутер, але над власними аліасами fake-* і без жодного імпорту з курсу.

- plan — службовий виклик, завжди на дешевій моделі (fake-mini);
- propose — за складністю: simple -> fake-mini, complex -> fake-strong;
- складність дає planner (поле complexity у JSON-плані), а задача довша за 300 символів — завжди complex.
"""

SIMPLE = "simple"
COMPLEX = "complex"

CHEAP_MODEL = "fake-mini"
STRONG_MODEL = "fake-strong"

# довга задача майже напевно зачіпає кілька місць сайту: не віддаємо її дешевій моделі
LONG_TASK_CHARS = 300

MODEL_BY_COMPLEXITY = {SIMPLE: CHEAP_MODEL, COMPLEX: STRONG_MODEL}

# recorded in hw2, executed in hw4: порядок fallback записано поруч із роутером, а крутитиме його
# конвеєр (reliability.run_chain) у hw4. Ланцюг починається з основної моделі й не повторює аліас.
AGENT_FALLBACK_CHAINS = {
    STRONG_MODEL: (STRONG_MODEL, CHEAP_MODEL),
    CHEAP_MODEL: (CHEAP_MODEL, STRONG_MODEL),
}


def task_complexity(task_text: str, planned: object) -> str:
    """Складність задачі: правило довжини сильніше за план; невідоме значення від planner -> simple
    (як «unknown» у таблиці намірів: незрозуміла задача однаково закінчиться needs_clarification)."""
    if len(task_text) > LONG_TASK_CHARS:
        return COMPLEX
    return planned if planned in MODEL_BY_COMPLEXITY else SIMPLE


def route_task(step: str, complexity: str | None = None) -> str:
    """Аліас моделі для кроку конвеєра."""
    if step == "plan":
        return CHEAP_MODEL
    return MODEL_BY_COMPLEXITY.get(complexity, CHEAP_MODEL)
