"""W3: кеш кроків агента — повтор без виклику моделі, точний ключ, лише успішні відповіді, real-hit без gateway."""

import pathlib
import time

from fastapi.testclient import TestClient

from sandbox.agent import pipeline
from sandbox.agent.cache import Entry, StepCache
from sandbox.main import app
from tests.agent.test_agent_real import TASK, real  # noqa: F401  (фікстура real)

client = TestClient(app)
ROOT = pathlib.Path(__file__).resolve().parents[2]


def post(**body):
    return client.post("/agent/tasks", json={"task": TASK, **body}).json()


def test_repeat_task_comes_from_cache_for_free(mem):
    first, second = post(), post()
    assert (first["calls"], first["cache_hits"]) == (2, 0) and first["cost_usd"] > 0
    assert (second["status"], second["calls"], second["cache_hits"], second["cost_usd"]) == ("proposed", 0, 2, 0.0)
    assert second["patch"]["diff"] == first["patch"]["diff"] and second["model"] == first["model"]
    rows = [c for c in mem.calls if str(c["task_id"]) == second["task_id"]]
    assert [(c["provider"], c["cache_hit"], c["prompt_tokens"], str(c["cost_usd"])) for c in rows] == \
        [("cache", True, 0, "0.000000")] * 2
    detail = client.get(f"/agent/tasks/{second['task_id']}").json()
    assert [c["cache_hit"] for c in detail["calls"]] == [True, True]
    s = client.get("/agent/cache").json()
    assert (s["entries"], s["hits"], s["misses"]) == (2, 2, 2) and s["saved_usd"] == first["cost_usd"]


def test_cache_false_goes_to_model(mem):
    post()
    d = post(cache=False)
    assert (d["calls"], d["cache_hits"], d["cache"]) == (2, 0, False)


def test_new_prompt_version_is_a_miss(mem):
    post()
    client.post("/agent/prompts/agent-editor/v1/activate", json={})
    d = post()
    assert d["cache_hits"] == 1 and d["calls"] == 1          # план — з кешу, propose з іншим промптом — до моделі
    client.post("/agent/prompts/agent-editor/rollback", json={})
    assert post()["cache_hits"] == 2                          # повернули v2 — знову той самий ключ


def test_failures_are_not_cached(mem):
    for _ in range(2):
        d = client.post("/agent/tasks", json={"task": "__fail_503 " + TASK}).json()
        assert (d["status"], d["calls"], d["cache_hits"]) == ("provider_error", 1, 0)
    assert client.get("/agent/cache").json()["entries"] == 0


def test_real_hit_sends_nothing(real, mem):
    a, b = post(mode="real"), post(mode="real")
    assert a["calls"] == 2 and (b["calls"], b["cache_hits"], b["status"]) == (0, 2, "proposed")
    assert len(real.requests) == 2                            # другий прогін не дійшов до gateway
    keys = {i["mode"] for i in client.get("/agent/cache").json()["items"]}
    assert keys == {"mock", "real"}                             # фікстура прогнала mock: mock і real — різні записи


def test_clear_endpoint(mem):
    post()
    assert client.post("/agent/cache/clear").json() == {"cleared": 2}
    assert post()["cache_hits"] == 0


def test_ttl_and_lru():
    c = StepCache(ttl_s=10, max_entries=2)
    for k in ("a", "b", "c"):
        c.put(Entry(k, "mock", "plan", "fake-mini", "p@v1", k, 1, 1, None))
    assert c.get("a") is None and c.get("b") is not None      # a витіснено (LRU)
    c._items["b"].created = time.time() - 11
    assert c.get("b") is None and c.snapshot()["entries"] == 1


def test_migration_is_additive_and_in_schema():
    body = (ROOT / "db" / "agent_migrations" / "hw3_cache.sql").read_text(encoding="utf-8")
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    assert body in schema and schema.index("-- === hw-i1 (кінець) ===") < schema.index(body)
    code = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("--"))
    assert code.strip() == "ALTER TABLE agent_calls ADD COLUMN IF NOT EXISTS cache_hit BOOLEAN NOT NULL DEFAULT false;"
    assert pipeline.step_cache.max_entries > 0
