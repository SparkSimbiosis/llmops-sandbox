"""Спільний контракт провайдера моделі: результат виклику — значення, а не виняток."""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(frozen=True)
class LLMResult:
    ok: bool                         # True лише для HTTP 200 з розбірливим тілом
    status: int                      # сирий HTTP-код; 0 — відповіді не було (мережа/таймаут)
    content: str                     # "" при content: null
    tool_calls: tuple = field(default_factory=tuple)   # ({"name": str, "arguments": str}, ...)
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    provider: str = "gateway"        # "gateway" | "fake" | "fault-injector" | "cache"
    model_alias: str = ""
    error: str | None = None         # "http_503" | "timeout" | "network" | "bad_json" | ...

    @property
    def retryable(self) -> bool:
        # 429, 5xx, немає відповіді, нерозбірливе тіло 200 — те, після чого має сенс інший провайдер (W4)
        return self.error == "bad_json" or self.status in (0, 429) or 500 <= self.status <= 599


class LLMProvider(Protocol):
    name: str

    async def complete(self, messages: list[dict], model_alias: str, *,
                       timeout_s: float, max_tokens: int | None = None, attempt: int = 1) -> LLMResult:
        """Ніколи не кидає виняток: будь-який збій повертається як LLMResult(ok=False, ...)."""
        ...
