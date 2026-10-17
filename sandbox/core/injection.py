"""Детектор prompt injection на вході (W4, опційно): три родини патернів з уроку 8, українською й англійською.

1. Скасування інструкцій: «ignore previous instructions», «забудь / ігноруй інструкції».
2. Витяг системного промпта: «print / reveal your system prompt», «покажи свій системний промпт».
3. Підміна ролі: «you are now …», «act as …», «developer mode», «тепер ти …», «уяви, що ти …».

Це не повний захист — лише перша лінія: mock розпізнає тільки канонічну англійську форму атаки,
а перефразовані варіанти ловимо тут, до роутера і до моделі. Збіг -> відмова без виклику моделі.
"""

import re

_FAMILIES = {
    "override": [
        r"\b(ignore|disregard|forget)\b.{0,40}\b(instruction|rule|previous|above)",
        r"(ігноруй|ігнорувати|забудь|не зважай на)\W.{0,40}(інструкц|правил|попередн)",
        r"(игнорируй|забудь)\W.{0,40}(инструкц|правил|предыдущ)",
    ],
    "exfiltrate": [
        r"\b(print|reveal|show|repeat|output)\b.{0,40}\bsystem\s*prompt",
        r"(покажи|виведи|повтори|роздрукуй).{0,40}(системн\w*\s+промпт|системн\w*\s+інструкц)",
        r"(покажи|выведи|повтори).{0,40}(системн\w*\s+промпт)",
    ],
    "role": [
        r"\byou are now\b", r"\bact as\b", r"\bdeveloper mode\b", r"\bjailbreak\b",
        r"(тепер ти|уяви, що ти|відтепер ти)",
    ],
}
_COMPILED = [(fam, re.compile(p, re.I | re.S)) for fam, pats in _FAMILIES.items() for p in pats]

REFUSAL_TEXT = "Вибачте, не можу виконати це прохання."


def detect_injection(text: str) -> str | None:
    """Назва родини атаки або None."""
    for family, pattern in _COMPILED:
        if pattern.search(text):
            return family
    return None
