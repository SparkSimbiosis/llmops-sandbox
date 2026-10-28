"""Маршрути dev-агента — усі під /agent/*. Курсова консоль їх не читає, курсові плитки від них не залежать.

hw1: реєстр агентських промптів (список, activate, rollback), файли фейкового сайту, задачі.
hw2: складність і вартість у задачах, GET /agent/cost (сума по agent_calls; курсовий /cost — окремо).
UI-0: сторінка стенда GET /agent/ui; редактор промптів — нова версія, текст версії, журнал активацій.
UI-1: GET /agent/projects — проєкти стенда для перемикача (поки активний лише demo-site).
I-1: /agent/instructions — реєстр інструкцій: імпорт read-only, версії, активна версія й share_with_model на проєкт.
B-1: /agent/studio — конструктор промптів: рушії, картки контрактів, сценарій і роутинг з YAML; save -> нова версія промпта.
W4: /agent/approvals — заявки на застосування diff (approve / reject, 409 stale); /agent/site/preview (CSP),
/agent/site/history, /agent/site/reset.
W5: /agent/chat — чат з агентом (читає сайт, зміни лише пропонує -> задача конвеєра -> заявка);
/agent/injections — пак промпт-ін'єкцій і його прогін (оцінка м'якого й жорсткого шарів).
W6: /agent/workspace — робочі простори (тестовий репозиторій, догфудинг llm-ops): індекс файлів, пакет контексту
й prompt-package без моделі (pack) і питання дослідникові з перевіркою посилань (ask), історія прогонів.
Недоступна агентська БД (зокрема відсутня схема agent_*) -> 503 {"error":"agent_db_unavailable"}.
"""

import json
import os
import pathlib
import uuid
from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from sandbox.agent import (approvals, builder, chat, compiler, configs, contextpack, explore, injections, instructions,
                           pipeline, pricing, routing, workspace)
from sandbox.agent.patches import diff_paths
from sandbox.agent.repo import AgentDbUnavailable
from sandbox.agent.settings import (AGENT_PROVIDER_MODE, AGENT_REAL_BUDGET_USD, EDITOR_PROMPT, PROJECTS,
                                    PROMPT_MAX_CHARS, TASK_MAX_CHARS, RealModeDisabled, resolve_mode)

router = APIRouter(prefix="/agent")

UI_DIR = pathlib.Path(__file__).resolve().parents[1] / "agent" / "ui"
UI_FILES = {"call-identity.js": "text/javascript", "demo-catalog.js": "text/javascript", "scenario-workspace.js": "text/javascript", "studio-guide.js": "text/javascript", "task-intake.js": "text/javascript", "studio-run.js": "text/javascript", "harness.js": "text/javascript", "app.js": "text/javascript", "app.css": "text/css"}
NO_CACHE = {"Cache-Control": "no-cache"}       # стенд оновлюється часто: браузер щоразу перепитує сервер

DB_DOWN = {"error": "agent_db_unavailable"}
NOT_FOUND = {"error": "not found"}


def _usd(value) -> float | None:
    # Decimal з NUMERIC -> JSON number; NULL лишається null (модель без ціни або відповіді не було)
    return None if value is None else float(value)


class TaskIn(BaseModel):
    task: str = Field(min_length=1, max_length=TASK_MAX_CHARS)
    mode: Literal["mock", "real"] | None = None
    base: Literal["current", "seed"] = "current"
    model: str | None = Field(default=None, max_length=64)    # ручний вибір моделі для propose; None — роутинг
    cache: bool = True                                         # W3: false — повз кеш кроків (свіжа відповідь)

    @field_validator("task")
    @classmethod
    def _no_nul(cls, v: str) -> str:
        # Postgres не зберігає NUL у text: краще чесні 422, ніж 503 «БД недоступна»
        if "\x00" in v:
            raise ValueError("task must not contain NUL")
        return v


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=TASK_MAX_CHARS)
    chat_id: str | None = None                                  # None — нова розмова
    mode: Literal["mock", "real"] | None = None
    cache: bool = True

    @field_validator("message")
    @classmethod
    def _no_nul(cls, v: str) -> str:
        if "\x00" in v or not v.strip():
            raise ValueError("message must be non-blank text without NUL")
        return v


class PackIn(BaseModel):
    ids: list[str] | None = None                                # None — увесь пак
    mode: Literal["mock", "real"] | None = None
    cache: bool = True


class PromptIn(BaseModel):
    body: str = Field(min_length=1, max_length=PROMPT_MAX_CHARS)

    @field_validator("body")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip() or "\x00" in v:
            raise ValueError("body must be non-blank text without NUL")
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


@router.post("/prompts/{name}")
async def create_prompt_version(name: str, body: PromptIn):
    """Нова версія vN+1 неактивною (activate — окремою дією). Той самий текст уже є — 200 з його версією."""
    try:
        result = await pipeline.registry.create(name, body.body)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    if result is None:
        return JSONResponse(NOT_FOUND, status_code=404)
    return JSONResponse(result, status_code=201 if result["created"] else 200)


@router.get("/prompts/{name}/activations")
async def prompt_activations(name: str, limit: int = Query(default=20, ge=1, le=100)):
    try:
        return {"name": name, "activations": await pipeline.registry.activations(name, limit)}
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)


@router.get("/prompts/{name}/{version}")
async def get_prompt_version(name: str, version: str):
    try:
        result = await pipeline.registry.get(name, version)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    if result is None:
        return JSONResponse(NOT_FOUND, status_code=404)
    return result


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


# превʼю сайту: жодного скрипта, зовнішнього ресурсу чи вбудовування в чужу сторінку — лише inline-стилі
PREVIEW_HEADERS = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                   "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


def _uuid(value: str | None):
    try:
        return None if value is None else uuid.UUID(value)
    except ValueError:
        return False


@router.get("/site/preview")
async def site_preview(approval: str | None = None):
    """Сайт як HTML: поточний або «після» заявки (dry-run, у БД нічого не пишеться)."""
    aid = _uuid(approval)
    if aid is False:
        return JSONResponse(NOT_FOUND, status_code=404)
    try:
        files, info = await approvals.site_texts(aid)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    if aid is not None and info is None:
        return JSONResponse(NOT_FOUND, status_code=404)
    return HTMLResponse(approvals.render(files), headers=PREVIEW_HEADERS)


@router.get("/site/history")
async def site_history(limit: int = Query(default=50, ge=1, le=200)):
    try:
        rows = await pipeline.repo.site_history(limit)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    return {"versions": [{"path": r["path"], "version": r["version"], "sha256": r["sha256"], "source": r["source"],
                          "task_id": str(r["source_task_id"]) if r["source_task_id"] else None,
                          "approval_id": str(r["approval_id"]) if r["approval_id"] else None,
                          "created_at": r["created_at"].isoformat()} for r in rows]}


@router.post("/site/reset")
async def site_reset():
    """Повертає seed новими версіями (append-only); відкриті заявки на старі версії стануть stale."""
    try:
        changed = await approvals.reset_site()
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    return {"reset": changed}


@router.get("/approvals")
async def list_approvals(status: Literal["pending", "applied", "rejected", "stale"] | None = None,
                         limit: int = Query(default=50, ge=1, le=200)):
    try:
        rows = await pipeline.repo.list_approvals(status, limit)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    return {"approvals": [approvals.summary(a) for a in rows]}


@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: str):
    aid = _uuid(approval_id)
    if not aid:
        return JSONResponse(NOT_FOUND, status_code=404)
    try:
        a = await pipeline.repo.get_approval(aid)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    if a is None:
        return JSONResponse(NOT_FOUND, status_code=404)
    return approvals.summary(a, with_diff=True)


@router.get("/approvals/{approval_id}/patch")
async def approval_patch(approval_id: str):
    """.patch схваленої заявки git-проєкту — як файл; застосовує людина (git apply), стенд у репозиторій не пише."""
    aid = _uuid(approval_id)
    if not aid:
        return JSONResponse(NOT_FOUND, status_code=404)
    try:
        a = await pipeline.repo.get_approval(aid)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    if a is None:
        return JSONResponse(NOT_FOUND, status_code=404)
    code, body = approvals.patch_file(a)
    if code != 200:
        return JSONResponse(body, status_code=code)
    name, text = body
    return PlainTextResponse(text, media_type="text/x-diff", headers={
        "Content-Disposition": f'attachment; filename="{name}"', "X-Content-Type-Options": "nosniff"})


@router.post("/approvals/{approval_id}/{decision}")
async def decide_approval(approval_id: str, decision: Literal["approve", "reject"], request: Request):
    aid = _uuid(approval_id)
    if not aid:
        return JSONResponse(NOT_FOUND, status_code=404)
    try:
        if decision == "approve":
            code, body = await approvals.approve(aid)
        else:
            code, body = await approvals.reject(aid, await _quote(request))
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    return JSONResponse(body, status_code=code)


@router.post("/tasks")
async def create_task(body: TaskIn):
    try:
        mode = resolve_mode(AGENT_PROVIDER_MODE, body.mode)
    except RealModeDisabled:
        # перевірка до будь-якого запису в БД: заблокований запит не лишає слідів
        return JSONResponse({"error": "real_mode_disabled"}, status_code=403)
    if body.model is not None and body.model not in routing.allowed_models(mode):
        # лише аліаси з routing.yaml для цього режиму: real-модель у mock (і навпаки) не пройде
        return JSONResponse({"error": "model_not_available", "mode": mode,
                             "allowed": sorted(routing.allowed_models(mode))}, status_code=422)
    status, payload = await pipeline.run_task(body.task, mode, body.base, body.model, body.cache)
    return JSONResponse(payload, status_code=status)


@router.post("/chat")
async def chat_post(body: ChatIn):
    try:
        mode = resolve_mode(AGENT_PROVIDER_MODE, body.mode)
    except RealModeDisabled:
        return JSONResponse({"error": "real_mode_disabled"}, status_code=403)
    cid = _uuid(body.chat_id) if body.chat_id is not None else uuid.uuid4()
    if cid is False:
        return JSONResponse({"error": "bad_chat_id"}, status_code=422)
    status, payload = await chat.chat_turn(cid, body.message, mode, body.cache)
    return JSONResponse(payload, status_code=status)


def _turn_row(t: dict) -> dict:
    return {"turn_id": str(t["turn_id"]), "seq": t["seq"], "created_at": t["created_at"].isoformat(),
            "mode": t["mode"], "user_text": t["user_text"], "status": t["status"], "reply": t["reply"],
            "proposal_text": t["proposal_text"], "task_id": t["task_id"] and str(t["task_id"]),
            "case_id": t["case_id"], "gates": t["gate_report"], "cost_usd": _usd(t["total_cost_usd"]),
            "error": t["error"], "approval_id": t["approval_id"] and str(t["approval_id"]),
            "approval_status": t["approval_status"],
            **{key: value for key, value in t.items() if key in
               {"own_calls", "calls", "own_cost_usd", "action_cost_usd", "action_calls",
                "action_cache_hits", "action_tokens", "accounting"}}}


@router.get("/chat")
async def chat_list(limit: int = Query(default=20, ge=1, le=100)):
    try:
        rows = await pipeline.repo.list_chats(limit)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    return {"chats": [{"chat_id": str(r["chat_id"]), "started_at": r["started_at"].isoformat(),
                       "last_at": r["last_at"].isoformat(), "turns": r["turns"], "first_text": r["first_text"],
                       "case_id": r["case_id"], "cost_usd": _usd(r["cost_usd"]),
                       **{k: r[k] for k in ("action_cost_usd", "real_estimate_usd", "synthetic_usd", "unknown_cost_calls")
                          if k in r}} for r in rows]}


@router.get("/chat/{chat_id}")
async def chat_get(chat_id: str):
    cid = _uuid(chat_id)
    if not cid:
        return JSONResponse(NOT_FOUND, status_code=404)
    try:
        turns = await pipeline.repo.get_chat(cid)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    if not turns:
        return JSONResponse(NOT_FOUND, status_code=404)
    return {"chat_id": str(cid), "turns": [_turn_row(t) for t in turns]}


@router.get("/injections")
async def injections_list():
    return {"cases": injections.catalog()}


@router.post("/injections/run")
async def injections_run(body: PackIn):
    try:
        mode = resolve_mode(AGENT_PROVIDER_MODE, body.mode)
    except RealModeDisabled:
        return JSONResponse({"error": "real_mode_disabled"}, status_code=403)
    unknown = [i for i in body.ids or [] if i not in injections.CASES]
    if unknown:
        return JSONResponse({"error": "unknown_case", "ids": unknown}, status_code=422)
    try:
        return await injections.run_pack(body.ids, mode, body.cache)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)


@router.get("/tasks")
async def list_tasks(limit: int = Query(default=20, ge=1, le=100)):
    try:
        rows = await pipeline.repo.list_tasks(limit)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    return {"tasks": [{"task_id": str(r["task_id"]), "created_at": r["created_at"].isoformat(),
                       "status": r["status"], "intent": r["intent"], "mode": r["mode"], "model": r["model"],
                       "cost_usd": _usd(r["total_cost_usd"])}     # задачі до міграції hw2 — null
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
              "completion_tokens": c["completion_tokens"], "cost_usd": _usd(c["cost_usd"]),
              "latency_ms": c["latency_ms"], "status": c["status"], "cache_hit": bool(c.get("cache_hit"))}
             for c in t["calls"]]
    return {"task_id": str(t["task_id"]), "created_at": t["created_at"].isoformat(),
            "updated_at": t["updated_at"].isoformat(), "status": t["status"], "task_text": t["task_text"],
            "intent": t["intent"], "complexity": t["complexity"], "mode": t["mode"], "base": t["base"],
            "prompt": {"name": EDITOR_PROMPT, "version": t["prompt_version"]},
            "model": calls[-1]["model"] if calls else None,
            "tokens": {"prompt": t["total_prompt_tokens"], "completion": t["total_completion_tokens"]},
            "cost_usd": _usd(t["total_cost_usd"]), "gates": t["gate_report"],
            "patch": None if diff is None else {"files": diff_paths(diff), "sha256": t["patch_sha256"], "diff": diff},
            "message": t["message"], "error": t["error"], "base_versions": t["base_versions"],
            "context_sha256": t["context_sha256"], "calls": calls,
            "approval_id": str(t["approval_id"]) if t.get("approval_id") else None}


@router.get("/cost")
async def agent_cost():
    """Вартість агента за сьогодні: SUM по agent_calls. Долари fake-моделей синтетичні (pricing.fake),
    тому бюджету для них немає; real_budget_usd — бюджет реальних грошей (до hw4 = 0)."""
    try:
        c = await pipeline.repo.cost_today()
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    from fastapi.encoders import jsonable_encoder
    return jsonable_encoder({**c, "pricing": pricing.PRICING,
                             "real_budget_usd": float(AGENT_REAL_BUDGET_USD)})


@router.get("/executions")
async def execution_list():
    from sandbox.agent.execution import Journal, JournalError
    try:
        grouped = {}
        for step in Journal().inspect():
            grouped.setdefault(step["run_id"], []).append(step)
        return {"executions": [{"run_id": key, "steps": value} for key, value in grouped.items()]}
    except (JournalError, OSError):
        return JSONResponse({"error": "execution_journal_unavailable"}, status_code=503)


@router.get("/executions/{run_id}")
async def execution_detail(run_id: str):
    from sandbox.agent.execution import Journal, JournalError
    try:
        steps = Journal().inspect(run_id)
        if not steps:
            return JSONResponse(NOT_FOUND, status_code=404)
        return {"run_id": run_id, "steps": steps}
    except (JournalError, OSError):
        return JSONResponse({"error": "execution_journal_unavailable"}, status_code=503)



# ---------- реєстр інструкцій (I-1) ----------

PROJECT_IDS = {p["id"] for p in PROJECTS}
Kind = Literal["role", "rule", "workflow", "contract", "skill"]


class ImportIn(BaseModel):
    source: Literal["sample", "local"]


class ProjectIn(BaseModel):
    project: str


class ShareIn(ProjectIn):
    share: bool


def _instructions() -> instructions.InstructionRegistry:
    # реєстр над тим самим репозиторієм агента, що й конвеєр (тести підміняють pipeline.repo)
    return instructions.InstructionRegistry(pipeline.repo)


def _bad_project(project: str) -> JSONResponse | None:
    return None if project in PROJECT_IDS else JSONResponse({"error": "unknown project"}, status_code=404)


@router.get("/instructions/sources")
async def instruction_sources():
    """Звідки можна імпортувати: приклад є завжди; локальний каталог — лише якщо змонтований (шлях не віддаємо)."""
    return {"sample": True, "local": instructions.local_dir() is not None}


@router.post("/instructions/import")
async def import_instructions(body: ImportIn):
    if body.source == "sample":
        root, overrides = instructions.SAMPLE_DIR, {}
    else:
        root = instructions.local_dir()
        if root is None:
            return JSONResponse({"error": "local instructions not mounted"}, status_code=409)
        overrides = instructions.parse_overrides(os.getenv("INSTRUCTIONS_KINDS"))
    try:
        return await _instructions().import_from(body.source, root, overrides)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)


@router.get("/instructions")
async def list_instructions(project: str = "demo-site"):
    if (bad := _bad_project(project)) is not None:
        return bad
    try:
        return {"project": project, "items": await _instructions().list(project)}
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)


@router.get("/instructions/{kind}/{name}")
async def instruction_versions(kind: Kind, name: str):
    try:
        rows = await _instructions().versions(kind, name)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    if not rows:
        return JSONResponse(NOT_FOUND, status_code=404)
    return {"kind": kind, "name": name, "versions": rows}


@router.get("/instructions/{kind}/{name}/{version}")
async def instruction_body(kind: Kind, name: str, version: str):
    try:
        row = await _instructions().get(kind, name, version)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    return row if row is not None else JSONResponse(NOT_FOUND, status_code=404)


@router.post("/instructions/{kind}/{name}/{version}/activate")
async def activate_instruction(kind: Kind, name: str, version: str, body: ProjectIn):
    if (bad := _bad_project(body.project)) is not None:
        return bad
    try:
        result = await _instructions().activate(body.project, kind, name, version)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    return result if result is not None else JSONResponse(NOT_FOUND, status_code=404)


@router.post("/instructions/{kind}/{name}/share")
async def share_instruction(kind: Kind, name: str, body: ShareIn):
    if (bad := _bad_project(body.project)) is not None:
        return bad
    try:
        result = await _instructions().share(body.project, kind, name, body.share)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    if result is None:
        return JSONResponse({"error": "no active version for project"}, status_code=409)
    return result


class DraftIn(BaseModel):
    title: str = Field(default="", max_length=200)
    task_goal: str = Field(default="", max_length=1000)
    background_context: str = Field(default="", max_length=1000)
    input_data: str = Field(default="", max_length=1000)
    constraints: list[str] = Field(default_factory=list, max_length=10)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=10)
    complexity: Literal["simple", "complex"] | None = None


class BuildIn(BaseModel):
    engine: str
    draft: DraftIn = Field(default_factory=DraftIn)


@router.get("/studio")
async def studio_catalog():
    """B-1: сценарій, рушії, картки контрактів і таблиця роутингу з YAML (без БД)."""
    return builder.catalog()


@router.post("/studio/build")
async def studio_build(body: BuildIn):
    """Зібрати промпт без збереження: текст, оцінка чернетки, рішення роутера і знімок у YAML."""
    result = builder.build(body.engine, body.draft.model_dump())
    return result if result is not None else JSONResponse(NOT_FOUND, status_code=404)


@router.post("/studio/save")
async def studio_save(body: BuildIn):
    """Зберегти зібраний промпт як нову неактивну версію цільового промпта рушія (activate — окремо)."""
    result = builder.build(body.engine, body.draft.model_dump())
    if result is None:
        return JSONResponse(NOT_FOUND, status_code=404)
    if not result["target_prompt"]:
        return JSONResponse({"error": "engine has no target prompt"}, status_code=409)
    if result["too_long"]:
        return JSONResponse({"error": "prompt too long", "chars": result["chars"]}, status_code=422)
    if not result["approvable"]:
        return JSONResponse({"error": "quality below threshold", "quality_score": result["review"]["quality_score"],
                             "approve_threshold": result["approve_threshold"]}, status_code=422)
    try:
        saved = await pipeline.registry.create(result["target_prompt"], result["prompt"])
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    if saved is None:
        return JSONResponse(NOT_FOUND, status_code=404)
    return JSONResponse({**saved, "route": result["route"]}, status_code=201 if saved["created"] else 200)


class CompileIn(BaseModel):
    agent: str
    task: str = Field(min_length=1, max_length=TASK_MAX_CHARS)
    mode: Literal["mock", "real"] = "mock"
    files: list[str] | None = Field(default=None, max_length=10)
    base: Literal["current", "seed"] = "current"
    patch: str | None = Field(default=None, max_length=20000)


@router.get("/cache")
async def cache_state():
    """W3: стан кешу кроків — записи, hit/miss, скільки заощаджено. Лише пам'ять процесу, БД не потрібна."""
    return pipeline.step_cache.snapshot()


@router.post("/cache/clear")
async def cache_clear():
    return {"cleared": pipeline.step_cache.clear()}


@router.get("/models")
async def list_models():
    """Моделі стенда: test-аліаси (фейкова модель) і real (DeepSeek, якщо ввімкнено в оточенні), класи й ціни."""
    return {"provider_mode": AGENT_PROVIDER_MODE, "models": routing.models(AGENT_PROVIDER_MODE)}


@router.get("/config")
async def config_catalog():
    """C-1: агенти, ролі, скіли, інструменти, рецепти контексту й файли конфігурації (без БД)."""
    return configs.catalog()


@router.post("/config/compile")
async def config_compile(body: CompileIn):
    """Зібрати виклик агента без моделі: блоки контексту, повідомлення, оцінка токенів і ціни, гейт."""
    try:
        result = await compiler.compile_call(body.agent, body.task, body.mode, body.files, body.base, body.patch)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    return result if result is not None else JSONResponse(NOT_FOUND, status_code=404)


@router.get("/projects")
async def list_projects():
    """UI-1: проєкти стенда й чи доступні вони зараз; режим провайдера — щоб UI чесно показав, чи є real."""
    return {"provider_mode": AGENT_PROVIDER_MODE, "projects": [dict(p) for p in PROJECTS]}


@router.get("/ui")
async def stand_ui():
    """Сторінка стенда: статичні файли з sandbox/agent/ui, без збирання. Ходить лише в /agent/*."""
    return FileResponse(UI_DIR / "index.html", media_type="text/html", headers=NO_CACHE)


@router.get("/ui/{name}")
async def stand_ui_asset(name: str):
    # явний список файлів, а не каталог: шлях із запиту ніколи не стає шляхом на диску
    if name not in UI_FILES:
        return JSONResponse(NOT_FOUND, status_code=404)
    return FileResponse(UI_DIR / name, media_type=UI_FILES[name], headers=NO_CACHE)


# ---------- робочі простори (W6) ----------

class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=TASK_MAX_CHARS)
    budget: int = Field(default=contextpack.BUDGET_DEFAULT, ge=contextpack.BUDGET_MIN, le=contextpack.BUDGET_MAX)
    pinned: list[str] = Field(default_factory=list, max_length=contextpack.PINNED_MAX)
    mode: Literal["mock", "real"] | None = None
    cache: bool = True

    @field_validator("question")
    @classmethod
    def _no_nul(cls, v: str) -> str:
        if "\x00" in v or not v.strip():
            raise ValueError("question must be non-blank text without NUL")
        return v


@router.get("/workspace")
async def workspace_projects():
    """Проєкти робочого простору: що доступно, білий список і жорсткі виключення."""
    return {"projects": workspace.projects(), "default": workspace.DEFAULT_PROJECT,
            "budget": {"default": contextpack.BUDGET_DEFAULT, "min": contextpack.BUDGET_MIN,
                       "max": contextpack.BUDGET_MAX}}


@router.get("/workspace/runs")
async def workspace_runs(limit: int = Query(default=20, ge=1, le=100)):
    try:
        rows = await pipeline.repo.list_context_runs(limit)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    return {"runs": [{**r, "run_id": str(r["run_id"]), "created_at": r["created_at"].isoformat(),
                      "total_cost_usd": _usd(r["total_cost_usd"])} for r in rows]}


@router.get("/workspace/runs/{run_id}")
async def workspace_run(run_id: str):
    rid = _uuid(run_id)
    if not rid:
        return JSONResponse(NOT_FOUND, status_code=404)
    try:
        r = await pipeline.repo.get_context_run(rid)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    if r is None:
        return JSONResponse(NOT_FOUND, status_code=404)
    current = workspace.index(r["project"])
    return {**{k: v for k, v in r.items() if k not in ("created_at", "updated_at", "total_cost_usd", "calls")},
            "run_id": str(r["run_id"]), "created_at": r["created_at"].isoformat(),
            "total_cost_usd": _usd(r["total_cost_usd"]),
            "calls": [{**c, "cost_usd": _usd(c["cost_usd"])} for c in r["calls"]],
            # чи той самий проєкт зараз: тексти фрагментів відтворюються лише з того самого знімка
            "snapshot_is_current": bool(current) and current["snapshot_sha256"] == r["snapshot_sha256"]}


@router.get("/workspace/{project}/files")
async def workspace_files(project: str):
    idx = workspace.index(project)
    return idx if idx is not None else JSONResponse({"error": "project not available"}, status_code=404)


@router.get("/workspace/{project}/file")
async def workspace_file(project: str, path: str = Query(min_length=1, max_length=400)):
    """Текст файлу — лише якщо він є в індексі проєкту (шлях із запиту не стає шляхом на диску)."""
    text = workspace.read_file(project, path)
    if text is None:
        return JSONResponse(NOT_FOUND, status_code=404)
    return {"project": project, "path": path, "text": text}


@router.post("/workspace/{project}/pack")
async def workspace_pack(project: str, body: AskIn):
    """Пакет контексту й prompt-package без виклику моделі: що саме піде в модель і скільки це коштуватиме."""
    try:
        mode = resolve_mode(AGENT_PROVIDER_MODE, body.mode)
    except RealModeDisabled:
        return JSONResponse({"error": "real_mode_disabled"}, status_code=403)
    try:
        prep = await explore.prepare(project, body.question, body.budget, body.pinned, mode)
    except AgentDbUnavailable:
        return JSONResponse(DB_DOWN, status_code=503)
    if prep is None:
        return JSONResponse({"error": "project not available"}, status_code=404)
    return {"project": project, "mode": mode, "pack": prep["pack"], "package": prep["package"]}


@router.post("/workspace/{project}/ask")
async def workspace_ask(project: str, body: AskIn):
    try:
        mode = resolve_mode(AGENT_PROVIDER_MODE, body.mode)
    except RealModeDisabled:
        return JSONResponse({"error": "real_mode_disabled"}, status_code=403)
    status, payload = await explore.ask_call(project, body.question, mode, body.budget, body.pinned, body.cache)
    return JSONResponse(payload, status_code=status)


# Keep all optional agent endpoints behind the existing guarded router.
from sandbox.agent.harness_routes import router as harness_router
router.include_router(harness_router)

from sandbox.agent.engine_routes import router as engine_router
router.include_router(engine_router)
