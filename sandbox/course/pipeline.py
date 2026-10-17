"""Оркестратор POST /chat — те саме місце, що й обробник /chat у стартері (service/Program.cs).

Шви тижнів позначені маркерами `# W2 routing`, `# W3 cache`, `# W4 fallback/HITL/PII`,
`# W5 observability` — за зразком TODO(student, W#) стартера, щоб ментор знаходив їх так само.
"""

import logging
from decimal import Decimal
from time import perf_counter
from uuid import uuid4

from sandbox.cache.response_cache import CACHE, CachedAnswer
from sandbox.core.config import DATABASE_URL, GATEWAY_TIMEOUT_S, GATEWAY_URL, MODEL
from sandbox.llm.gateway_provider import GatewayProvider
from sandbox.llm.pricing import cost_usd as price
from sandbox.llm.router import route
from sandbox.llm.types import Usage
from sandbox.observability.stats import STATS
from sandbox.prompts.registry import PROMPT_NAME, Registry
from sandbox.storage.db import Database, DbUnavailable
from sandbox.storage.repositories import CostRepo, PromptsRepo, RequestsRepo
from sandbox.tools.runtime import run_read_only

log = logging.getLogger("sandbox.chat")

UNAVAILABLE_TEXT = "Сервіс тимчасово недоступний."

db = Database(DATABASE_URL)
gateway = GatewayProvider(name="course", base_url=GATEWAY_URL)
requests_repo = RequestsRepo(db)
registry = Registry(PromptsRepo(db))
cost_repo = CostRepo(db)


async def handle_chat(message: str) -> tuple[int, dict]:
    """Повертає (HTTP-код, тіло) відповіді /chat. 200 у всіх бізнес-гілках; недоступна БД реєстру -> 503
    без рядка в логу: тихо підміняти реєстр дефолтом не можна, інакше поломку не видно."""
    t0 = perf_counter()
    request_id = uuid4()

    text = message                     # W4 fallback/HITL/PII: mask_pii(text) — першою операцією
    # W1: промпт — активна версія з реєстру; порожній реєстр -> чесний дефолт з версією 'none'
    try:
        active = await registry.get_active(PROMPT_NAME)
    except DbUnavailable:
        log.warning("chat request_id=%s prompt registry unavailable", request_id)
        return 503, {"request_id": str(request_id), "error": "prompt_registry_unavailable"}
    model = route(text, MODEL)         # W2 routing: дешева модель для звичайних питань, сильна — для ескалацій

    # W3 cache: повний ключ — модель + промпт (версія і sha тексту) + повідомлення
    key = CACHE.key(model, active.name, active.version, active.body, text)
    cached = CACHE.get(key)
    if cached is not None:
        STATS.hits += 1
        raw, tool, usage, status, cost_usd = cached.content, cached.tool, Usage(), 200, Decimal("0")
    else:
        STATS.misses += 1
        res = await gateway.complete(
            [{"role": "system", "content": active.body}, {"role": "user", "content": text}],
            model, timeout_s=GATEWAY_TIMEOUT_S)
        raw = res.content if res.ok else None
        tool = res.tool_calls[0]["name"] if res.ok and res.tool_calls else None
        usage, status = res.usage, res.status
        # W2 routing: tokens * ціна моделі; без відповіді моделі вартості немає (NULL), а не 0
        cost_usd = price(model, res.usage) if res.ok else None
        # кешуємо лише успішну відповідь моделі; дії з наслідками (create_ticket) — ніколи (HITL — hw4)
        if res.ok and tool != "create_ticket":
            CACHE.put(key, CachedAnswer(raw, tool))

    if raw is None:
        content = UNAVAILABLE_TEXT
    else:
        content = raw
        # W3 tools: read-only інструмент виконуємо (і на hit теж — дані можуть змінитися), результат — у відповідь
        result = run_read_only(tool) if tool else None
        if result is not None:
            content = f"{raw} ({result})"
    latency_ms = int((perf_counter() - t0) * 1000)

    # один рядок у requests ДО відповіді: з нього живуть observability і cost;
    # prompt_version — саме та версія, що пішла в модель; на hit токени й вартість нульові
    await requests_repo.insert_safe(request_id, model, active, latency_ms, usage, cost_usd, str(status))
                                       # W5 observability: лічильники
    log.info("chat request_id=%s model=%s prompt_version=%s status=%s cache=%s latency_ms=%d",
             request_id, model, active.version, status, "hit" if cached else "miss", latency_ms)
    return 200, {"request_id": str(request_id), "content": content, "tool": tool, "latency_ms": latency_ms}
