"""Маршрути курсу. Форми відповідей — ті, що читає готова консоль (ui/src/app/*.ts)."""

from fastapi import APIRouter
from pydantic import BaseModel

from sandbox.course import pipeline

router = APIRouter()


class ChatIn(BaseModel):
    message: str


@router.post("/chat")
async def chat(body: ChatIn):
    return await pipeline.handle_chat(body.message)


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
    return {"todo": "sum cost_usd for today + budget"}  # W2


@router.get("/prompts")
async def prompts():
    return {"todo": "list from prompts table"}          # W1


@router.get("/providers")
async def providers():
    return {"todo": "provider health"}                  # W5


@router.get("/approvals")
async def approvals():
    return {"todo": "pending HITL approvals"}           # W4
