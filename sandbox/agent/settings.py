"""Налаштування dev-агента. Значення за замовчуванням — у коді, у docker-compose.yml нічого не додаємо."""

import logging
import os

log = logging.getLogger("sandbox.agent")

# hw1: обидва кроки (plan і propose) йдуть на дешеву модель; маршрутизація за складністю — з hw2
AGENT_MODEL = "fake-mini"

PLANNER_PROMPT = "agent-planner"
EDITOR_PROMPT = "agent-editor"

# єдині файли, які агент може пропонувати змінити: явний список, а не префікс каталогу,
# щоб шлях на кшталт demo-site/../x не пройшов навіть теоретично
ALLOWED_PATHS = (
    "demo-site/index.html",
    "demo-site/header.html",
    "demo-site/footer.html",
    "demo-site/styles.css",
)

TASK_MAX_CHARS = 2000

# власний лінивий пул агента: малий, щоб агент не міг забрати з'єднання в курсу
AGENT_DB_MAX_SIZE = 3
AGENT_DB_ATTEMPTS = 3
AGENT_DB_PAUSE_S = 0.5

# фейковий провайдер відповідає миттєво; таймаут лишаємо в контракті на майбутній REAL-режим
AGENT_CALL_TIMEOUT_S = 30.0


def _int_env(name: str, default: int) -> int:
    # кривий рядок в оточенні не повинен валити імпорт агента — беремо дефолт
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        log.warning("agent setting %s is not an integer, using %d", name, default)
        return default


# маркер __delay: неблокуюча пауза в мілісекундах (у тестах 0)
AGENT_FAKE_DELAY_MS = _int_env("AGENT_FAKE_DELAY_MS", 2000)


class RealModeDisabled(Exception):
    """Запит просить mode=real, а реальний режим вимкнено -> 403 {"error":"real_mode_disabled"}."""


def env_mode(raw: str | None) -> str:
    """Режим провайдера з оточення. До hw4 коду REAL немає: 'real' в оточенні ігноруємо з WARN,
    щоб випадкове значення не відкрило шлях до реальних грошей."""
    if raw == "real":
        log.warning("AGENT_PROVIDER_MODE=real is ignored until hw4, using mock")
        return "mock"
    return "mock"


AGENT_PROVIDER_MODE = env_mode(os.getenv("AGENT_PROVIDER_MODE", "mock"))


def resolve_mode(env: str, requested: str | None) -> str:
    """Понизити до mock можна завжди; real — лише якщо його ввімкнено в оточенні.
    У hw1 env завжди 'mock' (див. env_mode), тому запит з "mode":"real" отримує 403."""
    env = env if env in ("mock", "real") else "mock"
    if requested is None:
        return env
    if requested == "mock":
        return "mock"
    if requested == "real" and env == "real":
        return "real"
    raise RealModeDisabled
