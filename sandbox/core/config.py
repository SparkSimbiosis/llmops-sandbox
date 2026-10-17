"""Налаштування беремо з оточення (задаються в docker-compose.yml), як у стартері.

MODEL=mock — дефолт, грошей не треба. Реальна модель (gpt-5-mini, claude-haiku) потребує ключа
в gateway/.env; сервіс ключів не бачить — їх тримає лише gateway.
"""

import os

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://gateway:4000")
# asyncpg приймає лише URI, тому замість ADO.NET-рядка стартера (DB_CONN) — DATABASE_URL
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://llmops:llmops@postgres:5432/llmops")
MODEL = os.getenv("MODEL", "mock")
BUDGET_USD = float(os.getenv("BUDGET_USD", "5.0"))
# як таймаут HttpClient за замовчуванням у стартері
GATEWAY_TIMEOUT_S = 100.0
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
