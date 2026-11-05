"""Реєстр агентських промптів: той самий механізм уроку W1, що й у курсу, але на власній таблиці.

Початкові версії — з міграції (db/schema.sql); редактор UI додає нові через POST /agent/prompts/{name}:
лише наступну vN+1, неактивною, без зміни наявних (редагування = нова версія). У кожен рядок agent_calls пишеться версія і sha256 тексту,
який справді пішов у модель.
"""

from dataclasses import dataclass

from sandbox.core.hashing import sha256_hex

# чесний дефолт, коли активної версії немає: формату відповіді в ньому немає, тож фейкова модель
# не дає ні плану, ні diff — поломку реєстру видно в статусі задачі, а не замасковано
AGENT_FALLBACK_PROMPT = "You are an assistant. No agent prompt version is active in the registry."

ACTOR = "anonymous"


@dataclass(frozen=True)
class AgentPrompt:
    name: str
    version: str          # 'v1' | 'v2' | 'none' (реєстр порожній — спрацював дефолт)
    version_id: object    # UUID | None
    body: str
    sha256: str


def fallback(name: str) -> AgentPrompt:
    return AgentPrompt(name, "none", None, AGENT_FALLBACK_PROMPT, sha256_hex(AGENT_FALLBACK_PROMPT))


class AgentPromptRegistry:
    def __init__(self, repo):
        self._repo = repo

    async def get_active(self, name: str) -> AgentPrompt:
        """Один SELECT на крок. Порожній реєстр — fallback з версією 'none';
        недоступна БД — AgentDbUnavailable з репозиторію (тихої підміни немає)."""
        row = await self._repo.fetch_active_prompt(name)
        if row is None:
            return fallback(name)
        version, body, version_id = row
        return AgentPrompt(name, version, version_id, body, sha256_hex(body))

    async def list(self) -> list[dict]:
        return [{"name": r["name"], "version": r["version"], "active": r["active"],
                 "version_id": str(r["version_id"]), "sha256": sha256_hex(r["body"])}
                for r in await self._repo.list_prompts()]

    async def activate(self, name: str, version: str, quote: str | None = None) -> dict | None:
        """None — такої версії немає: нічого не змінено, аудиту немає (маршрут відповідає 404)."""
        return _shape(name, await self._repo.activate_prompt(name, version, ACTOR, quote))

    async def rollback(self, name: str, quote: str | None = None) -> dict | None:
        """None — повертатися нема куди (маршрут відповідає 409)."""
        return _shape(name, await self._repo.rollback_prompt(name, ACTOR, quote))

    async def get(self, name: str, version: str) -> dict | None:
        r = await self._repo.get_prompt(name, version)
        if r is None:
            return None
        return {"name": r["name"], "version": r["version"], "active": r["active"],
                "version_id": str(r["version_id"]), "sha256": sha256_hex(r["body"]), "body": r["body"],
                "created_at": r["created_at"].isoformat()}

    async def create(self, name: str, body: str) -> dict | None:
        """None — невідоме ім'я (маршрут відповідає 404). created=false — такий текст уже є в реєстрі."""
        result = await self._repo.create_prompt_version(name, body)
        if result is None:
            return None
        version, created = result
        return {"name": name, "version": version, "sha256": sha256_hex(body), "created": created}

    async def activations(self, name: str, limit: int = 20) -> "list[dict]":  # list тут — метод класу
        return [{"version": a["version"], "previous_version": a["previous_version"], "actor": a["actor"],
                 "quote": a["approval_quote"], "activated_at": a["activated_at"].isoformat()}
                for a in await self._repo.list_activations(name, limit)]


def _shape(name: str, result) -> dict | None:
    if result is None:
        return None
    version, previous = result
    return {"name": name, "version": version, "active": True, "previous_version": previous}
