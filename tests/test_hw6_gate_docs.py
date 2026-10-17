"""hw6: поріг гейта в двох місцях, крок evals не глушиться, ранбук і rollout на місці; (опц.) /metrics."""

import pathlib
import re

from fastapi.testclient import TestClient

from sandbox.course import pipeline
from sandbox.main import app
from sandbox.observability.stats import STATS
from sandbox.storage.db import DbUnavailable

ROOT = pathlib.Path(__file__).resolve().parents[1]
WF = (ROOT / ".github/workflows/eval-gate.yml").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_threshold_9_in_workflow_and_readme():
    assert re.search(r"--threshold 9\b", WF) and not re.search(r"--threshold [0-8]\b", WF)
    assert "--threshold 9" in README
    assert WF.splitlines()[48].strip().startswith("run: python evals/run.py")   # рядок 49


def test_eval_step_is_not_silenced():
    assert "continue-on-error" not in WF and "|| true" not in WF.split("name: Evals")[1].split("- name:")[0]


def test_runbook_and_rollout():
    rb = (ROOT / "INCIDENT_RUNBOOK.md").read_text(encoding="utf-8")
    assert "__fail_503" in rb and "date +%s" in rb and "..." not in rb.replace('"..."', "")
    assert "INCIDENT_RUNBOOK.md" in README
    rollout = README.split("#### 30-day rollout")[1].split("####")[0]
    assert len(re.findall(r"^\d\. \*\*Тиждень", rollout, re.M)) == 4


class Obs:
    async def today(self):
        return {"requests": 5, "p95_ms": 12, "error_rate_pct": 20.0}


class Cost:
    async def today_usd(self):
        return 0.0012


class Down:
    async def today(self):
        raise DbUnavailable("down")


def test_metrics_prometheus_format(monkeypatch):
    monkeypatch.setattr(pipeline, "obs_repo", Obs())
    monkeypatch.setattr(pipeline, "cost_repo", Cost())
    STATS.hits, STATS.misses = 1, 1
    STATS.errors["provider_5xx"] = 3
    r = TestClient(app).get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    body = r.text
    for line in ("sandbox_requests_today 5", "sandbox_cache_hit_pct 50.0", 'sandbox_errors_total{reason="provider_5xx"} 3',
                 "sandbox_cost_today_usd 0.0012"):
        assert line in body


def test_metrics_without_db_still_serves_counters(monkeypatch):
    monkeypatch.setattr(pipeline, "obs_repo", Down())
    body = TestClient(app).get("/metrics").text
    assert "sandbox_cache_hits_total" in body and "sandbox_requests_today" not in body
