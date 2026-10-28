"""Explicit prepare/run API for the local harness preview."""
from typing import Literal
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sandbox.agent import harness, passports
from sandbox.agent.repo import AgentDbUnavailable
from sandbox.agent.settings import RealModeDisabled

router = APIRouter()

class PrepareIn(BaseModel):
    project: Literal["demo-site", "notes-app", "llm-ops"]
    goal: str = Field(min_length=1, max_length=2000)
    profile: Literal["quick", "thorough"] = "quick"
    environment: Literal["lab", "staging", "production"] = "lab"
    complexity: Literal["easy", "medium", "hard"] = "easy"
    risk: Literal["normal", "critical"] = "normal"
    mode: Literal["mock", "real"] = "mock"
    cache: bool = True
    kind: Literal["read", "change"] = "read"

class RunIn(BaseModel):
    preparation_id: str = Field(min_length=36, max_length=36)
    confirmed: bool = False

@router.get("/harness/projects")
async def projects():
    return {"projects": harness.projects()}

@router.get("/projects/{project}/passport")
async def passport(project: str, profile: str = "quick", environment: str = "lab", complexity: str = "easy"):
    try:
        return passports.passport(project, profile, environment, complexity)
    except passports.PassportConfigError as exc:
        return JSONResponse({"error": "invalid_passport", "message": str(exc)}, status_code=422)

async def _respond(call):
    try:
        return await call
    except harness.HarnessError as exc:
        return JSONResponse({"error": exc.code}, status_code=exc.status)
    except passports.PassportConfigError:
        return JSONResponse({"error": "invalid_passport"}, status_code=422)
    except AgentDbUnavailable:
        return JSONResponse({"error": "agent_db_unavailable", "retry": "inspect execution journal before preparing again"}, status_code=503)
    except RealModeDisabled:
        return JSONResponse({"error": "real_mode_disabled"}, status_code=403)

@router.post("/harness/prepare")
async def prepare(body: PrepareIn):
    return await _respond(harness.prepare(**body.model_dump()))

@router.post("/harness/run")
async def run(body: RunIn):
    return await _respond(harness.run(body.preparation_id, body.confirmed))
