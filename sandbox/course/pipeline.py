"""Оркестратор POST /chat — те саме місце, що й обробник /chat у стартері (service/Program.cs).

Шви тижнів позначені маркерами `# W2 routing`, `# W3 cache`, `# W4 fallback/HITL/PII`,
`# W5 observability` — за зразком TODO(student, W#) стартера, щоб ментор знаходив їх так само.
"""

import logging
from time import perf_counter
from uuid import uuid4

from sandbox.core.config import DATABASE_URL, GATEWAY_TIMEOUT_S, GATEWAY_URL, MODEL
from sandbox.llm.gateway_provider import GatewayProvider
from sandbox.storage.db import Database
from sandbox.storage.repositories import RequestsRepo

log = logging.getLogger("sandbox.chat")

# W1: промпт поки захардкоджений, як у стартері; наступний коміт бере його з реєстру
SYSTEM_PROMPT = "You are a support assistant."
UNAVAILABLE_TEXT = "Сервіс тимчасово недоступний."

db = Database(DATABASE_URL)
gateway = GatewayProvider(name="course", base_url=GATEWAY_URL)
requests_repo = RequestsRepo(db)


async def handle_chat(message: str) -> dict:
    t0 = perf_counter()
    request_id = uuid4()

    text = message                     # W4 fallback/HITL/PII: mask_pii(text) — першою операцією
    model = MODEL                      # W2 routing: route(text, MODEL)
    system_prompt = SYSTEM_PROMPT
                                       # W3 cache: lookup за повним ключем (модель, промпт, повідомлення)
    res = await gateway.complete(
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": text}],
        model, timeout_s=GATEWAY_TIMEOUT_S)

    content = res.content if res.ok else UNAVAILABLE_TEXT
    # модель попросила інструмент — поки лише читаємо назву, не виконуємо (виконання — W3/W4)
    tool = res.tool_calls[0]["name"] if res.ok and res.tool_calls else None
    latency_ms = int((perf_counter() - t0) * 1000)
    cost_usd = None                    # W2 routing: tokens * ціна моделі -> cost_usd

    # один рядок у requests ДО відповіді: з нього живуть observability і cost
    await requests_repo.insert_safe(request_id, model, latency_ms, res.usage, cost_usd, str(res.status))
                                       # W5 observability: лічильники
    log.info("chat request_id=%s model=%s status=%s latency_ms=%d", request_id, model, res.status, latency_ms)
    return {"request_id": str(request_id), "content": content, "tool": tool, "latency_ms": latency_ms}
