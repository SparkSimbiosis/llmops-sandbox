"""LLMRouter dev-агента: обирає провайдера за режимом. Курс ним не користується — курсовий /chat
викликає GatewayProvider напряму.

hw1: лише mock -> FakeProvider. Гілка real (BudgetGuard -> FaultInjector -> GatewayProvider("agent"))
з'явиться в hw4 і за замовчуванням буде вимкнена; до того resolve_mode відсікає real ще на вході (403).
Ланцюг fallback крутиться в конвеєрі, а не тут: роутер і провайдери без стану.
"""

from sandbox.llm.fake_provider import FakeProvider
from sandbox.llm.types import LLMResult


class LLMRouter:
    def __init__(self, fake: FakeProvider, *, timeout_s: float = 30.0):
        self._fake = fake
        self._timeout_s = timeout_s

    async def complete(self, messages: list[dict], model_alias: str, mode: str, *,
                       attempt: int = 1, max_tokens: int | None = None) -> LLMResult:
        if mode == "mock":
            return await self._fake.complete(messages, model_alias, timeout_s=self._timeout_s,
                                             max_tokens=max_tokens, attempt=attempt)
        # реального провайдера в hw1 немає: відповідаємо невдачею без жодного мережевого виклику
        return LLMResult(ok=False, status=0, content="", provider="gateway",
                         model_alias=model_alias, error="real_mode_disabled")
