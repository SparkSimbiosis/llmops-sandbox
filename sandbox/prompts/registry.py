"""Реєстр промптів (W1). Єдине місце, яке читає таблицю prompts і збирає з неї system-промпт:
пайплайн /chat тексту промпта не тримає, лише питає тут активну версію.
Версії наповнює міграція db/schema.sql — сервіс їх тільки читає і перемикає active.
"""

import asyncio
import re
from dataclasses import dataclass

from sandbox.core.hashing import sha256_hex
from sandbox.storage.db import DbUnavailable

# промпт, який бере /chat. Назва — лише ключ у таблиці, у текст повідомлень вона не потрапляє.
PROMPT_NAME = "support-system"

# чесний дефолт, коли активної версії немає. Навмисно без слова «support»: mock тоді
# відповідає «не знаю», evals червоніють — поломку реєстру видно, а не замасковано.
FALLBACK_PROMPT = "You are an assistant. No prompt version is active in the registry."

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


@dataclass(frozen=True)
class ActivePrompt:
    """Версія, яка реально пішла в модель. body — вже відрендерений текст system-повідомлення,
    sha256 — від цього самого тексту: по логу видно, що саме бачила модель."""
    name: str
    version: str
    version_id: object  # UUID | None (None — реєстр порожній, спрацював дефолт)
    body: str
    sha256: str


EMPTY = ActivePrompt(PROMPT_NAME, "none", None, FALLBACK_PROMPT, sha256_hex(FALLBACK_PROMPT))


def render(template: str, variables: dict[str, str]) -> str:
    """{{name}} -> значення за один прохід: підставлене значення вдруге не розбирається.
    Невідомий плейсхолдер — виняток, а не тихий порожній рядок у промпті."""

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in variables:
            raise KeyError(f"unknown prompt placeholder: {key}")
        return variables[key]

    return _PLACEHOLDER.sub(sub, template)


class Registry:
    def __init__(self, repo, *, attempts: int = 3, pause_s: float = 0.5):
        self._repo = repo
        self._attempts = attempts
        self._pause_s = pause_s

    async def get_active(self, name: str = PROMPT_NAME) -> ActivePrompt:
        """Один SELECT на запит. Порожній реєстр — це EMPTY ('none'), а недоступна БД — DbUnavailable:
        тихого fallback немає, пайплайн відповідає 503."""
        for attempt in range(1, self._attempts + 1):
            try:
                row = await self._repo.fetch_active(name)
                break
            except DbUnavailable:
                if attempt == self._attempts:
                    raise
                # postgres міг ще не піднятися (depends_on не чекає готовності)
                await asyncio.sleep(self._pause_s)
        if row is None:
            return EMPTY
        version, body, version_id = row
        text = render(body, {})  # змінних у шаблонах поки немає — для v1/v2 нічого не змінює
        return ActivePrompt(name, version, version_id, text, sha256_hex(text))

    async def list_versions(self) -> list[dict]:
        return [_row_json(r) for r in await self._repo.list_all()]

    async def activate(self, version: str, quote: str | None = None) -> dict | None:
        """promote / rollback: активною стає рівно одна версія — одна транзакція на все.
        None — такої версії немає: нічого не змінено і подію активації не записано."""
        result = await self._repo.activate(PROMPT_NAME, version, "anonymous", quote)
        if result is None:
            return None
        previous, rows = result
        return {"name": PROMPT_NAME, "version": version, "previous_version": previous,
                "versions": [_row_json(r) for r in rows]}


def _row_json(r: dict) -> dict:
    # форма відповіді: snake_case; консоль читає name / version / active
    return {"name": r["name"], "version": r["version"], "active": r["active"],
            "version_id": str(r["version_id"]), "sha256": sha256_hex(r["body"]),
            "created_at": r["created_at"].isoformat()}
