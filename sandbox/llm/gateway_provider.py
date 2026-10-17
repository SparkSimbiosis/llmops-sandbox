"""Виклик моделі через LiteLLM gateway (OpenAI-сумісний HTTP).

Сервіс — control plane: LiteLLM лише ходить у модель, а рішення (яку модель брати, коли перемикатися,
скільки коштує) ухвалює наш код. Ключів провайдерів тут немає — їх тримає gateway.
"""

from time import perf_counter

import httpx

from sandbox.llm.types import LLMResult, Usage


class GatewayProvider:
    def __init__(self, name: str, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        # name — лише мітка екземпляра для логів ("course" | "agent"); provider у результаті завжди "gateway"
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._transport = transport  # у тестах — httpx.MockTransport, у роботі — справжня мережа

    async def complete(self, messages: list[dict], model_alias: str, *,
                       timeout_s: float, max_tokens: int | None = None, attempt: int = 1) -> LLMResult:
        # рівно ключі model і messages, як у стартері; max_tokens — лише якщо його передали
        payload: dict = {"model": model_alias, "messages": messages}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        t0 = perf_counter()

        def done(**kw) -> LLMResult:
            return LLMResult(latency_ms=int((perf_counter() - t0) * 1000), provider="gateway",
                             model_alias=model_alias, **kw)

        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=timeout_s) as client:
                resp = await client.post(f"{self._base_url}/v1/chat/completions", json=payload)
        except httpx.TimeoutException:
            return done(ok=False, status=0, content="", error="timeout")
        except Exception:
            return done(ok=False, status=0, content="", error="network")

        if resp.status_code != 200:
            return done(ok=False, status=resp.status_code, content="", error=f"http_{resp.status_code}")

        try:
            data = resp.json()
            message = data["choices"][0]["message"]
            if "content" not in message:
                raise KeyError("content")
            content = message["content"] or ""          # content: null -> "" (як у стартері)
            tool_calls = tuple(
                {"name": tc["function"]["name"], "arguments": tc["function"].get("arguments", "")}
                for tc in (message.get("tool_calls") or []))
            usage = Usage(int(data["usage"]["prompt_tokens"]), int(data["usage"]["completion_tokens"]))
        except Exception:
            # 200 без content або usage чи битий JSON — у стартері це виняток і текст недоступності
            return done(ok=False, status=200, content="", error="bad_json")

        return done(ok=True, status=200, content=content, tool_calls=tool_calls, usage=usage)
