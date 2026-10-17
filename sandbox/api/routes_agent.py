"""Маршрути dev-агента — усі під /agent/*. Курсова консоль їх не читає, курсові плитки від них не залежать.

hw1: реєстр агентських промптів (список, activate, rollback), файли фейкового сайту, задачі.
Недоступна агентська БД (зокрема відсутня схема agent_*) -> 503 {"error":"agent_db_unavailable"}.
"""

import json
import uuid
from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from sandbox.agent import pipeline
from sandbox.agent.patches import diff_paths
from sandbox.agent.repo import AgentDbUnavailable
from sandbox.agent.settings import AGENT_PROVIDER_MODE, EDITOR_PROMPT, TASK_MAX_CHARS, RealModeDisabled, resolve_mode

router = APIRouter(prefix="/agent")

DB_DOWN = {"error": "agent_db_unavailable"}
NOT_FOUND = {"error": "not found"}


class TaskIn(BaseModel):
    task: str = Field(min_length=1, max_length=TASK_MAX_CHARS)
    mode: Literal["mock", "real"] | None = None
    base: Literal["current", "seed"] = "current"

    @field_validator("task")
    @classmethod
    def _no_nul(cls, v: str) -> str:
        # Postgres не зберігає NUL у text: краще чесні 422, ніж 503 «БД недоступна»
        if "\x00" in v:
            raise ValueError("task must not contain NUL")
        return v


async def _quote(request: Request) -> str | None:
    # тіло необов'язкове: {} або {"quote": "..."}; невалідне тіло не заважає дії (як у курсу)
    try:
        data = json.loads(await request.body() or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None
    return data["quote"] if isinstance(data, dict) and isinstance(data.get("quote"), str) else None


@router.get("/prompts")
async def list_prompts():
    try:
        return {"prompts": await pipeline.registry.list()}
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)


@router.post("/prompts/{name}/{version}/activate")
async def activate_prompt(name: str, version: str, request: Request):
    quote = await _quote(request)
    try:
        result = await pipeline.registry.activate(name, version, quote)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    if result is None:
        # невідома версія: нічого не змінено, активна лишається активною
        return JSONResponse(NOT_FOUND, status_code=404)
    return result


@router.post("/prompts/{name}/rollback")
async def rollback_prompt(name: str, request: Request):
    quote = await _quote(request)
    try:
        result = await pipeline.registry.rollback(name, quote)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    if result is None:
        return JSONResponse({"error": "no previous version"}, status_code=409)
    return result


@router.get("/site/files")
async def list_site_files():
    try:
        rows = await pipeline.repo.list_site_files()
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    # sha256 — збережений у БД при записі версії, а не перерахований тут
    return {"files": [{"path": r["path"], "version": r["version"], "sha256": r["sha256"], "bytes": r["bytes"]}
                      for r in rows]}


@router.get("/site/files/{path:path}")
async def get_site_file(path: str, version: int | None = Query(default=None, ge=1)):
    try:
        row = await pipeline.repo.get_site_file(path, version)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    if row is None:
        return JSONResponse(NOT_FOUND, status_code=404)
    return {"path": row["path"], "version": row["version"], "sha256": row["sha256"], "content": row["content"]}


@router.post("/tasks")
async def create_task(body: TaskIn):
    try:
        mode = resolve_mode(AGENT_PROVIDER_MODE, body.mode)
    except RealModeDisabled:
        # перевірка до будь-якого запису в БД: заблокований запит не лишає слідів
        return JSONResponse({"error": "real_mode_disabled"}, status_code=403)
    status, payload = await pipeline.run_task(body.task, mode, body.base)
    return JSONResponse(payload, status_code=status)


@router.get("/tasks")
async def list_tasks(limit: int = Query(default=20, ge=1, le=100)):
    try:
        rows = await pipeline.repo.list_tasks(limit)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    return {"tasks": [{"task_id": str(r["task_id"]), "created_at": r["created_at"].isoformat(),
                       "status": r["status"], "intent": r["intent"], "model": r["model"],
                       "cost_usd": None}            # вартість — з hw2
                      for r in rows]}


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    try:
        tid = uuid.UUID(task_id)
    except ValueError:
        return JSONResponse(NOT_FOUND, status_code=404)
    try:
        t = await pipeline.repo.get_task(tid)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    if t is None:
        return JSONResponse(NOT_FOUND, status_code=404)
    diff = t["patch_diff"]
    calls = [{"seq": c["seq"], "step": c["step"],
              "attempt": 1,                 # fallback-спроби з'являться в hw4; до того кожен крок — одна спроба
              "provider": c["provider"], "model": c["model"], "prompt_version": c["prompt_version"],
              "prompt_sha256": c["prompt_sha256"], "prompt_tokens": c["prompt_tokens"],
              "completion_tokens": c["completion_tokens"], "latency_ms": c["latency_ms"], "status": c["status"]}
             for c in t["calls"]]
    return {"task_id": str(t["task_id"]), "created_at": t["created_at"].isoformat(),
            "updated_at": t["updated_at"].isoformat(), "status": t["status"], "task_text": t["task_text"],
            "intent": t["intent"], "mode": t["mode"], "base": t["base"],
            "prompt": {"name": EDITOR_PROMPT, "version": t["prompt_version"]},
            "model": calls[-1]["model"] if calls else None,
            "tokens": {"prompt": t["total_prompt_tokens"], "completion": t["total_completion_tokens"]},
            "cost_usd": None, "gates": t["gate_report"],
            "patch": None if diff is None else {"files": diff_paths(diff), "sha256": t["patch_sha256"], "diff": diff},
            "message": t["message"], "error": t["error"], "base_versions": t["base_versions"],
            "context_sha256": t["context_sha256"], "calls": calls}
