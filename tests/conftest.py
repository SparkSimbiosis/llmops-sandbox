"""Спільне для всіх тестів: кеш і лічильники курсу живуть у пам'яті процесу — скидаємо їх перед кожним тестом."""

import pytest

from sandbox.approvals.queue import QUEUE
from sandbox.cache.response_cache import CACHE
from sandbox.tools.runtime import TICKETS
from sandbox.observability.stats import STATS


@pytest.fixture(autouse=True)
def _fresh_course_memory():
    CACHE.entries.clear()
    CACHE.epoch = 0
    STATS.hits = STATS.misses = STATS.fallback_events = STATS.consecutive_failures = 0
    QUEUE._pending.clear(); QUEUE._completed.clear(); TICKETS.clear()
    yield
