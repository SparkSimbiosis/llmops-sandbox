"""Точка входу сервісу: uvicorn sandbox.main:app (порт 8080, як у стартері)."""

import logging
import os

from fastapi import FastAPI

from sandbox.api.routes_course import router as course_router
from sandbox.core.config import LOG_LEVEL, MODEL
from sandbox.llm.router import check_model

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("sandbox")

check_model(MODEL)  # без ланцюга fallback для MODEL сервіс не стартує

# на старті до БД не ходимо: пул створюється ліниво при першому запиті (storage/db.py)
app = FastAPI(title="sandbox")
app.include_router(course_router)

# dev-agent поза курсом: AGENT_ENABLED=0 вимикає /agent/*, а збій імпорту агента не валить курс
if os.getenv("AGENT_ENABLED", "1") == "1":
    try:
        from sandbox.api.routes_agent import router as agent_router
        app.include_router(agent_router)
    except Exception:
        log.exception("agent router disabled")
