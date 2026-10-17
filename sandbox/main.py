"""Точка входу сервісу: uvicorn sandbox.main:app (порт 8080, як у стартері)."""

import logging

from fastapi import FastAPI

from sandbox.api.routes_course import router as course_router
from sandbox.core.config import LOG_LEVEL

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("sandbox")

# на старті до БД не ходимо: пул створюється ліниво при першому запиті (storage/db.py)
app = FastAPI(title="sandbox")
app.include_router(course_router)
