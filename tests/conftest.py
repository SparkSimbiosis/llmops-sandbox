"""Спільне для всіх тестів: кеш і лічильники курсу живуть у пам'яті процесу — скидаємо їх перед кожним тестом."""

import pytest

from sandbox.agent import pipeline as agent_pipeline
from sandbox.approvals.queue import QUEUE
from sandbox.cache.response_cache import CACHE
from sandbox.llm.breaker import BREAKER
from sandbox.tools.runtime import TICKETS
from sandbox.observability.stats import STATS


@pytest.fixture(autouse=True)
def _fresh_course_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_EXECUTION_DIR", str(tmp_path / "executions"))
    CACHE.entries.clear()
    CACHE.epoch = 0
    STATS.__init__()   # усі лічильники з нуля, включно з помилками за причинами
    QUEUE._pending.clear(); QUEUE._completed.clear(); QUEUE._expired.clear(); TICKETS.clear()
    BREAKER.states.clear()
    agent_pipeline.step_cache.__init__(agent_pipeline.step_cache.ttl_s, agent_pipeline.step_cache.max_entries)   # W3: кеш кроків агента
    yield
