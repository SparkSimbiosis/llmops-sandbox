"""Гейт вихідного контексту (R-1): що саме піде в реальну модель — перевіряємо до відправки.

Кожне повідомлення real-виклику сканується на секрети (ті самі патерни, що й pre-push скан) і на
приватні імена (локальний denylist). Збіг — виклику немає, задача blocked_by_context_gate.
Denylist у репозиторії не живе: його монтують read-only у каталог AGENT_GATE_DIR. Немає denylist —
гейт закритий (fail-closed): real без перевірки приватних імен не працює.
У звіт іде лише назва правила й номер повідомлення — сам збіг не повторюємо ні в БД, ні в лозі.
"""

import logging
import os
import pathlib
import re
from dataclasses import dataclass

log = logging.getLogger("sandbox.agent.outgoing")

# вбудований мінімум — копія secret-patterns pre-push скану (POSIX-класи переписано на \s)
BUILTIN_SECRETS = (
    r"sk-[A-Za-z0-9_-]{20,}",
    r"sk-ant-[A-Za-z0-9_-]{10,}",
    r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"(api[_-]?key|secret|token|password)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/_+=-]{16,}",
    r"postgres(ql)?://[^:/ ]+:[^@ ]+@",
)
SECRET_FILE = "secret-patterns.txt"
DENYLIST_FILES = ("denylist.txt", "denylist-words.txt")


@dataclass(frozen=True)
class Rules:
    secrets: tuple[re.Pattern, ...]
    denylist: tuple[re.Pattern, ...]

    @property
    def ready(self) -> bool:
        return bool(self.denylist)


def _lines(path: pathlib.Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _compile(raw: str, flags: int) -> re.Pattern | None:
    try:
        return re.compile(raw.replace("[[:space:]]", r"\s"), flags)
    except re.error:
        log.warning("outgoing gate: pattern skipped (bad regex)")   # сам патерн не логуємо — він приватний
        return None


def load(gate_dir: str | None = None) -> Rules:
    raw_dir = gate_dir if gate_dir is not None else os.getenv("AGENT_GATE_DIR", "")
    root = pathlib.Path(raw_dir) if raw_dir else None
    secret_src = list(BUILTIN_SECRETS) + (_lines(root / SECRET_FILE) if root else [])
    deny_src = [line for name in DENYLIST_FILES for line in _lines(root / name)] if root else []
    secrets = tuple(p for p in (_compile(s, 0) for s in dict.fromkeys(secret_src)) if p)
    denylist = tuple(_compile(d, re.IGNORECASE) for d in dict.fromkeys(deny_src))
    if None in denylist:
        # хоч одне ім'я не перевіряється -> гейт закритий цілком, а не «майже весь»
        log.warning("outgoing gate: denylist has a bad entry, gate closed")
        return Rules(secrets, ())
    return Rules(secrets, denylist)


def check(messages: list[dict], rules: Rules) -> list[dict]:
    """Порожній список — можна відправляти. Інакше — що спрацювало (без тексту збігу)."""
    if not rules.ready:
        return [{"rule": "gate_not_configured", "message": None}]
    hits = []
    for i, m in enumerate(messages):
        text = str(m.get("content") or "")
        if any(p.search(text) for p in rules.secrets):
            hits.append({"rule": "secret_pattern", "message": i})
        if any(p.search(text) for p in rules.denylist):
            hits.append({"rule": "denylist", "message": i})
    return hits
