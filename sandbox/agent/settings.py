"""Налаштування dev-агента. Значення за замовчуванням — у коді, у docker-compose.yml нічого не додаємо."""

import logging
import os

log = logging.getLogger("sandbox.agent")

# hw2: модель для кожного кроку обирає sandbox/agent/routing.py (plan -> fake-mini, propose — за складністю)

PLANNER_PROMPT = "agent-planner"
EDITOR_PROMPT = "agent-editor"
CHAT_PROMPT = "agent-chat"
EXPLORE_PROMPT = "agent-explorer"

# єдині файли, які агент може пропонувати змінити: явний список, а не префікс каталогу,
# щоб шлях на кшталт demo-site/../x не пройшов навіть теоретично
ALLOWED_PATHS = (
    "demo-site/index.html",
    "demo-site/header.html",
    "demo-site/footer.html",
    "demo-site/styles.css",
)

# W4: ліміти одного diff (гейт size_limits)
PATCH_MAX_FILES = 3
PATCH_MAX_CHANGED_LINES = 40
PATCH_MAX_BYTES = 20_000

# UI-1: проєкти стенда для задач конвеєра (розділ 17.3). Задачі конвеєра — лише demo-site; git-проєкти (W6)
# читаються на вкладці «Проєкт», а зміна для LLM-Ops — це .patch через Harness (sandbox/agent/repopatch.py)
PROJECTS = (
    {"id": "demo-site", "title": "Демо-сайт", "kind": "fake", "available": True, "reason": None},
    {"id": "llm-ops", "title": "LLM-Ops (догфудинг)", "kind": "git", "available": False,
     "reason": "зміни — .patch у сценарії «Зміна з підтвердженням»; питання — вкладка «Проєкт»"},
    {"id": "test-repo", "title": "Тестовий репозиторій", "kind": "git", "available": False,
     "reason": "лише питання — вкладка «Проєкт»; зміни для цього проєкту вимкнено паспортом"},
)

TASK_MAX_CHARS = 2000
# W5: чат — скільки попередніх ходів бачить модель і довжина пропозиції, що йде в конвеєр задачею
CHAT_HISTORY_TURNS = 6
CHAT_PROPOSAL_MAX_CHARS = 500
PROMPT_MAX_CHARS = 4000     # тіло нової версії промпта з редактора UI

# власний лінивий пул агента: малий, щоб агент не міг забрати з'єднання в курсу
AGENT_DB_MAX_SIZE = 3
AGENT_DB_ATTEMPTS = 3
AGENT_DB_PAUSE_S = 0.5

# фейковий провайдер відповідає миттєво; real (R-1) — живий DeepSeek через gateway, відповідь editor буває довгою
AGENT_CALL_TIMEOUT_S = 60.0


def _int_env(name: str, default: int) -> int:
    # кривий рядок в оточенні не повинен валити імпорт агента — беремо дефолт
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        log.warning("agent setting %s is not an integer, using %d", name, default)
        return default


# R-1: денний бюджет реальних грошей агента. Стеля $1 закріплена в CARD R-1: більше з оточення не підняти,
# менше (зокрема 0 — real заблоковано) — можна.
AGENT_REAL_BUDGET_CAP_USD = 1.0


def real_budget(raw: str | None) -> float:
    try:
        value = float(raw) if raw not in (None, "") else AGENT_REAL_BUDGET_CAP_USD
    except ValueError:
        log.warning("AGENT_REAL_BUDGET_USD is not a number, using %s", AGENT_REAL_BUDGET_CAP_USD)
        value = AGENT_REAL_BUDGET_CAP_USD
    return max(0.0, min(value, AGENT_REAL_BUDGET_CAP_USD))


AGENT_REAL_BUDGET_USD = real_budget(os.getenv("AGENT_REAL_BUDGET_USD"))
# скільки токенів відповіді дозволяємо real-виклику: обмежує найгіршу ціну одного кроку
AGENT_REAL_MAX_TOKENS = 2000

# W3: кеш кроків у пам'яті процесу — скільки живе відповідь і скільки відповідей тримаємо (LRU)
AGENT_CACHE_TTL_S = _int_env("AGENT_CACHE_TTL_S", 3600)
AGENT_CACHE_MAX = _int_env("AGENT_CACHE_MAX", 200)

# маркер __delay: неблокуюча пауза в мілісекундах (у тестах 0)
AGENT_FAKE_DELAY_MS = _int_env("AGENT_FAKE_DELAY_MS", 2000)


class RealModeDisabled(Exception):
    """Запит просить mode=real, а реальний режим вимкнено -> 403 {"error":"real_mode_disabled"}."""


def env_mode(raw: str | None) -> str:
    """Режим провайдера з оточення (R-1): 'real' вмикає реальну модель, будь-що інше — mock.
    Навіть увімкнений real застосовується лише до задач, які явно попросили "mode":"real"."""
    if raw == "real":
        return "real"
    if raw not in (None, "", "mock"):
        log.warning("AGENT_PROVIDER_MODE=%r is unknown, using mock", raw)
    return "mock"


AGENT_PROVIDER_MODE = env_mode(os.getenv("AGENT_PROVIDER_MODE", "mock"))


def resolve_mode(env: str, requested: str | None) -> str:
    """Понизити до mock можна завжди; real — лише явним запитом і лише якщо його ввімкнено в оточенні.
    Задача без mode — завжди mock: реальні гроші ніколи не витрачаються «за замовчуванням»."""
    env = env if env in ("mock", "real") else "mock"
    if requested is None:
        return "mock"
    if requested == "mock":
        return "mock"
    if requested == "real" and env == "real":
        return "real"
    raise RealModeDisabled
