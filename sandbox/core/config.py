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

# Реальна модель (опційно): порожньо — усе на mock, як у перевірках курсу і в CI.
# REAL_MODEL=deepseek-chat (дешева) або deepseek-reasoner (сильна) — третій маршрут «все інше» йде в неї.
REAL_MODEL = os.getenv("REAL_MODEL", "").strip()
REAL_BUDGET_USD = float(os.getenv("REAL_BUDGET_USD", "1.0"))   # денний ліміт на реальну модель
REAL_MAX_TOKENS = int(os.getenv("REAL_MAX_TOKENS", "400"))     # стеля відповіді — контроль вартості
CACHE_TTL_S = float(os.getenv("CACHE_TTL_S", "3600"))          # (опц. hw3) скільки живе запис кешу
TOOL_TIMEOUT_S = float(os.getenv("TOOL_TIMEOUT_S", "3.0"))      # (опц. hw3) timeout на інструмент
