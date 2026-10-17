"""Маршрути курсу. Форми відповідей — ті, що читає готова консоль (ui/src/app/*.ts)."""

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from sandbox.cache.response_cache import CACHE
from sandbox.core.config import BUDGET_USD
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
    return {"todo": "aggregate from requests table"}   # W5


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


@router.get("/providers")
async def providers():
    return {"todo": "provider health"}                  # W5


@router.get("/approvals")
async def approvals():
    return {"todo": "pending HITL approvals"}           # W4
