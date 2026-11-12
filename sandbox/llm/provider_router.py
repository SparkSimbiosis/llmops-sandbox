"""LLMRouter dev-агента: обирає провайдера за режимом. Курс ним не користується — курсовий /chat
викликає GatewayProvider напряму.

mock -> FakeProvider. real (R-1) -> GatewayProvider("agent") -> LiteLLM gateway -> DeepSeek; ключ тримає
gateway, сервіс його не бачить. Бюджет і гейт вихідного контексту перевіряє конвеєр ДО виклику роутера.
Без gateway (не передали) real відповідає невдачею без жодного мережевого виклику.
Ланцюг fallback крутиться в конвеєрі, а не тут: роутер і провайдери без стану.
"""

from sandbox.llm.fake_provider import FakeProvider
from sandbox.llm.types import LLMProvider, LLMResult


class LLMRouter:
    def __init__(self, fake: FakeProvider, *, gateway: LLMProvider | None = None, timeout_s: float = 30.0):
        self._fake = fake
        self._gateway = gateway
        self._timeout_s = timeout_s

    async def complete(self, messages: list[dict], model_alias: str, mode: str, *,
                       attempt: int = 1, max_tokens: int | None = None) -> LLMResult:
        if mode == "mock":
            return await self._fake.complete(messages, model_alias, timeout_s=self._timeout_s,
                                             max_tokens=max_tokens, attempt=attempt)
        if self._gateway is None:
            return LLMResult(ok=False, status=0, content="", provider="gateway",
                             model_alias=model_alias, error="real_mode_disabled")
        return await self._gateway.complete(messages, model_alias, timeout_s=self._timeout_s,
                                            max_tokens=max_tokens, attempt=attempt)
