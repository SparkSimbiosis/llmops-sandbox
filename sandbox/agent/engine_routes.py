"""Prepared Engine routes included by the optional /agent router."""
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sandbox.agent import engine_runtime, passports
from sandbox.agent.repo import AgentDbUnavailable
from sandbox.agent.settings import RealModeDisabled

router = APIRouter()

class EngineDraft(BaseModel):
    title: str = Field(default="", max_length=200)
    task_goal: str = Field(default="", max_length=1000)
    background_context: str = Field(default="", max_length=1000)
    input_data: str = Field(default="", max_length=1000)
    constraints: list[str] = Field(default_factory=list, max_length=10)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=10)
    complexity: Literal["simple", "complex"] | None = None

class PrepareIn(BaseModel):
    engine: str = Field(min_length=1, max_length=64)
    draft: EngineDraft = Field(default_factory=EngineDraft)
    mode: Literal["mock", "real"] = "mock"
    profile: Literal["quick", "thorough"] = "quick"
    project: Literal["demo-site", "notes-app", "llm-ops"] | None = None
    cache: bool = True

class RunIn(BaseModel):
    preparation_id: UUID

async def _respond(call):
    try:
        return await call
    except engine_runtime.EngineError as exc:
        return JSONResponse({"error": exc.code}, status_code=exc.status)
    except passports.PassportConfigError:
        return JSONResponse({"error": "invalid_profile"}, status_code=422)
    except AgentDbUnavailable:
        return JSONResponse({"error": "agent_db_unavailable", "retry": "inspect journal before preparing another run"}, status_code=503)
    except RealModeDisabled:
        return JSONResponse({"error": "real_mode_disabled"}, status_code=403)

@router.post("/studio/prepare")
async def prepare(body: PrepareIn):
    return await _respond(engine_runtime.prepare(**body.model_dump()))

@router.post("/studio/run")
async def run(body: RunIn):
    return await _respond(engine_runtime.run(str(body.preparation_id)))

@router.get("/studio/runs")
async def list_runs(limit: int = Query(default=30, ge=1, le=50)):
    return await _respond(engine_runtime.list_runs(limit))

@router.get("/studio/runs/{run_id}")
async def get_run(run_id: UUID):
    result = await _respond(engine_runtime.get_run(run_id))
    return JSONResponse({"error": "not_found"}, status_code=404) if result is None else result
