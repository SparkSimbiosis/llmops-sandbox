"""Оркестратор POST /chat — те саме місце, що й обробник /chat у стартері (service/Program.cs).

Шви тижнів позначені маркерами `# W2 routing`, `# W3 cache`, `# W4 fallback/HITL/PII`,
`# W5 observability` — за зразком TODO(student, W#) стартера, щоб ментор знаходив їх так само.
"""

import logging
from decimal import Decimal
from time import perf_counter
from uuid import uuid4

from sandbox.approvals.queue import QUEUE
from sandbox.cache.response_cache import CACHE, CachedAnswer
from sandbox.core.injection import REFUSAL_TEXT, detect_injection
from sandbox.core.pii import mask_pii
from sandbox.core.config import DATABASE_URL, GATEWAY_TIMEOUT_S, GATEWAY_URL, MODEL
from sandbox.llm.gateway_provider import GatewayProvider
from sandbox.llm.pricing import cost_usd as price
from sandbox.llm.router import FALLBACK_CHAINS, route
from sandbox.llm.breaker import BREAKER
from sandbox.llm.types import LLMResult, Usage
from sandbox.observability.stats import STATS
from sandbox.prompts.registry import PROMPT_NAME, Registry
from sandbox.storage.db import Database, DbUnavailable
from sandbox.storage.repositories import CostRepo, PromptsRepo, RequestsRepo
from sandbox.tools.runtime import run_read_only

log = logging.getLogger("sandbox.chat")

HITL_TEXT = "Запит на створення тікета передано оператору; тікет буде створено після підтвердження."
DEGRADATION_TEXT = "Вибачте, зараз тимчасові труднощі з обробкою запиту. Будь ласка, спробуйте трохи пізніше."

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

    # W4 fallback/HITL/PII: маскування email — першою операцією, до роутера, ключа кешу і моделі
    text = mask_pii(message)
    # W1: промпт — активна версія з реєстру; порожній реєстр -> чесний дефолт з версією 'none'
    try:
        active = await registry.get_active(PROMPT_NAME)
    except DbUnavailable:
        log.warning("chat request_id=%s prompt registry unavailable", request_id)
        return 503, {"request_id": str(request_id), "error": "prompt_registry_unavailable"}
    # W4 (опц.) injection: перефразовані атаки ловимо до роутера й моделі — відмова без виклику моделі
    family = detect_injection(text)
    if family is not None:
        model = route(text, MODEL)
        latency_ms = int((perf_counter() - t0) * 1000)
        await requests_repo.insert_safe(request_id, model, active, latency_ms, Usage(), Decimal("0"), "200")
        log.info("chat request_id=%s model=%s prompt_version=%s status=200 injection=%s latency_ms=%d",
                 request_id, model, active.version, family, latency_ms)
        return 200, {"request_id": str(request_id), "content": REFUSAL_TEXT, "tool": None, "latency_ms": latency_ms}

    model = route(text, MODEL)         # W2 routing: дешева модель для звичайних питань, сильна — для ескалацій

    # W3 cache: повний ключ — модель + промпт (версія і sha тексту) + повідомлення
    key = CACHE.key(model, active.name, active.version, active.body, text)
    cached = CACHE.get(key)
    if cached is not None:
        STATS.hits += 1
        raw, tool, usage, status, cost_usd = cached.content, cached.tool, Usage(), 200, Decimal("0")
    else:
        STATS.misses += 1
        # W4 fallback/HITL/PII: ланцюг моделей з роутера (hw2); 429/5xx/немає відповіді -> наступна модель
        messages = [{"role": "system", "content": active.body}, {"role": "user", "content": text}]
        res = None
        for i, alias in enumerate(FALLBACK_CHAINS[model]):
            if i > 0:
                STATS.fallback_events += 1     # кожне перемикання — плитка «Fallback» (hw5)
            if not BREAKER.allow(alias):
                continue                       # (опц.) breaker відкритий: модель не смикаємо, йдемо далі
            res = await gateway.complete(messages, alias, timeout_s=GATEWAY_TIMEOUT_S)
            if res.ok:
                BREAKER.success(alias)
                break
            if not res.retryable:
                break
            BREAKER.failure(alias)
        if res is None:
            # усі моделі ланцюга з відкритим breaker: у модель не ходили, одразу заглушка
            res = LLMResult(ok=False, status=503, content="", model_alias=FALLBACK_CHAINS[model][-1],
                            error="circuit_open")
        if res.ok:
            STATS.consecutive_failures = 0     # будь-яка справжня відповідь провайдера скидає серію збоїв
        else:
            STATS.consecutive_failures += 1    # впав увесь ланцюг (для /providers, hw5)
        model = res.model_alias                # у лог — модель, яка фактично відповідала (або остання спробувана)
        raw = res.content if res.ok else None
        tool = res.tool_calls[0]["name"] if res.ok and res.tool_calls else None
        usage = res.usage
        # повний відказ ланцюга: 502 для нерозбірливої відповіді, інакше код останньої спроби (0, 429, 5xx)
        status = res.status if res.ok else (502 if res.error == "bad_json" else res.status)
        # W2 routing: tokens * ціна моделі; без відповіді моделі вартості немає (NULL), а не 0
        cost_usd = price(model, res.usage) if res.ok else None
        # кешуємо лише успішну відповідь моделі; дії з наслідками (create_ticket) — ніколи (HITL — hw4)
        if res.ok and tool != "create_ticket":
            CACHE.put(key, CachedAnswer(raw, tool))

    if raw is None:
        # власний текст без вигаданих фактів: ні статусу замовлення, ні номера тікета, ні «зроблено»
        content = DEGRADATION_TEXT
    else:
        content = raw
        # W3 tools: read-only інструмент виконуємо (і на hit теж — дані можуть змінитися), результат — у відповідь
        result = run_read_only(tool) if tool else None
        if tool == "create_ticket":
            # W4 HITL: незворотна дія не виконується в /chat — заявка йде в чергу /approvals
            QUEUE.add("create_ticket", str(request_id))
            content = HITL_TEXT
        if result is not None:
            content = f"{raw} ({result})"
    latency_ms = int((perf_counter() - t0) * 1000)

    # один рядок у requests ДО відповіді: з нього живуть observability і cost;
    # prompt_version — саме та версія, що пішла в модель; на hit токени й вартість нульові
    await requests_repo.insert_safe(request_id, model, active, latency_ms, usage, cost_usd, str(status))
                                       # W5 observability: лічильники
    log.info("chat request_id=%s model=%s prompt_version=%s status=%s cache=%s fallback=%d latency_ms=%d",
             request_id, model, active.version, status, "hit" if cached else "miss", STATS.fallback_events, latency_ms)
    return 200, {"request_id": str(request_id), "content": content, "tool": tool, "latency_ms": latency_ms}
