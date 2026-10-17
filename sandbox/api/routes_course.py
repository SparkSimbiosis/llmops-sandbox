"""Маршрути курсу. Форми відповідей — ті, що читає готова консоль (ui/src/app/*.ts)."""

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from sandbox.approvals.queue import QUEUE
from sandbox.cache.response_cache import CACHE
from sandbox.core.config import BUDGET_USD, MODEL, REAL_MODEL
from sandbox.llm.breaker import BREAKER
from sandbox.llm.router import FALLBACK_CHAINS, providers_for
from sandbox.observability.stats import STATS
from sandbox.course import pipeline
from sandbox.storage.db import DbUnavailable

router = APIRouter()


class ChatIn(BaseModel):
    message: str


@router.post("/chat")
async def chat(body: ChatIn):
    status, payload = await pipeline.handle_chat(body.message)
    return JSONResponse(payload, status_code=status)


@router.get("/health")
async def health():
    # лівнес, не для консолі; від БД не залежить
    return {"status": "ok"}


# заглушки, які читає консоль: поки форма {"todo": ...}, плитка показує «—».
# Кожна оживає у свій тиждень — текст дослівно як у стартері.
@router.get("/observability")
async def observability():
    # W5: три числа з БД (чесні — звіряються SQL) + два лічильники з пам'яті процесу (після рестарту з нуля)
    try:
        db_part = await pipeline.obs_repo.today()
    except DbUnavailable:
        return JSONResponse({"error": "db_unavailable"}, status_code=503)
    return {**db_part, "cache_hit_pct": STATS.cache_hit_pct(), "fallback_events": STATS.fallback_events,
            # (опц.) розбивка помилок за причинами — консоль її не читає, це для розбору інцидентів
            "errors_by_reason": dict(STATS.errors), "semantic_hits": STATS.semantic_hits}


@router.get("/cost")
async def cost():
    # W2: плитка «Вартість сьогодні»; числа, не рядки — консоль робить toFixed(2)
    try:
        today = await pipeline.cost_repo.today_usd()
    except DbUnavailable:
        return JSONResponse({"error": "db_unavailable"}, status_code=503)
    return {"today_usd": today, "budget_usd": BUDGET_USD}


@router.get("/prompts")
async def prompts():
    # W1: список версій реєстру; у кожній видно, активна вона чи ні (картка Prompt registry)
    try:
        return await pipeline.registry.list_versions()
    except DbUnavailable:
        return JSONResponse({"error": "prompt_registry_unavailable"}, status_code=503)


@router.post("/prompts/{version}/activate")
async def activate_prompt(version: str, request: Request):
    # promote / rollback. Тіло необов'язкове: консоль шле {}, можна {"quote": "..."} — чиє рішення
    # і дослівно яке (йде в аудит). Невалідне тіло не заважає активації.
    quote = None
    try:
        data = json.loads(await request.body() or b"{}")
        if isinstance(data, dict) and isinstance(data.get("quote"), str):
            quote = data["quote"]
    except (ValueError, UnicodeDecodeError):
        pass
    try:
        result = await pipeline.registry.activate(version, quote)
    except DbUnavailable:
        return JSONResponse({"error": "prompt_registry_unavailable"}, status_code=503)
    if result is None:
        # неіснуюча версія: 404 і нічого не змінено — активна лишається активною
        return JSONResponse({"error": "prompt_version_not_found", "version": version}, status_code=404)
    CACHE.invalidate()  # W3: новий промпт — старі відповіді з кешу вже не про нього
    return result


@router.get("/metrics")
async def metrics():
    # (опц. hw6) ті самі показники у форматі Prometheus — для зовнішнього дашборда (Grafana, profile advanced)
    lines = [
        "# TYPE sandbox_cache_hits_total counter", f"sandbox_cache_hits_total {STATS.hits}",
        "# TYPE sandbox_cache_misses_total counter", f"sandbox_cache_misses_total {STATS.misses}",
        "# TYPE sandbox_semantic_hits_total counter", f"sandbox_semantic_hits_total {STATS.semantic_hits}",
        "# TYPE sandbox_cache_hit_pct gauge", f"sandbox_cache_hit_pct {STATS.cache_hit_pct()}",
        "# TYPE sandbox_fallback_events_total counter", f"sandbox_fallback_events_total {STATS.fallback_events}",
        "# TYPE sandbox_consecutive_failures gauge", f"sandbox_consecutive_failures {STATS.consecutive_failures}",
        "# TYPE sandbox_errors_total counter",
        *[f'sandbox_errors_total{{reason="{r}"}} {n}' for r, n in sorted(STATS.errors.items())],
    ]
    try:
        today = await pipeline.obs_repo.today()
        cost = await pipeline.cost_repo.today_usd()
        lines += ["# TYPE sandbox_requests_today gauge", f"sandbox_requests_today {today['requests']}",
                  "# TYPE sandbox_p95_ms_today gauge", f"sandbox_p95_ms_today {today['p95_ms']}",
                  "# TYPE sandbox_error_rate_pct_today gauge", f"sandbox_error_rate_pct_today {today['error_rate_pct']}",
                  "# TYPE sandbox_cost_today_usd gauge", f"sandbox_cost_today_usd {cost}"]
    except DbUnavailable:
        pass  # БД недоступна — віддаємо лише лічильники з пам'яті; Prometheus покаже прогалину, а не брехню
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@router.get("/providers")
async def providers():
    # W5: статус реагує на збої — після 3 повних відмов ланцюга поспіль усі моделі degraded;
    # модель з відкритим circuit breaker теж degraded. Порядок — як у ланцюгах роутера.
    names = providers_for(MODEL) + (FALLBACK_CHAINS[REAL_MODEL] if REAL_MODEL else [])
    seen, items = set(), []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        bad = STATS.consecutive_failures >= 3 or BREAKER.is_open(name)
        items.append({"name": name, "status": "degraded" if bad else "ok"})
    return {"providers": items}


@router.get("/approvals")
async def approvals():
    # W4: картка Approvals · HITL читає pending[].id і .action
    return QUEUE.snapshot()


@router.post("/approvals/{approval_id}/approve")
async def approve(approval_id: str):
    item = QUEUE.approve(approval_id)
    if item is None:
        # повтор або невідомий id: дія вже спожита або її не було — другого тікета немає
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"id": item.id, "action": item.action, "result": item.result}
