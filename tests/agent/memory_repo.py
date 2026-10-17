"""Агентський репозиторій у пам'яті з тією ж семантикою, що AgentRepo над Postgres (тести без БД).

Seed читається з агентського блоку db/schema.sql: тести бачать ті самі промпти й файли demo-site,
що й живий том, тож очікування (ханки diff, sha) рахуються на справжньому seed.
"""

import pathlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sandbox.agent.repo import AgentDbUnavailable
from sandbox.core.hashing import sha256_hex

SCHEMA = pathlib.Path(__file__).resolve().parents[2] / "db" / "schema.sql"
BEGIN = "-- === hw1: agent (початок) ==="
END = "-- === hw1: agent (кінець) ==="


def agent_block() -> str:
    text = SCHEMA.read_text(encoding="utf-8")
    return text[text.index(BEGIN):text.index(END) + len(END)]


def seed_prompts() -> list[tuple[str, str, str, bool]]:
    rows = re.findall(r"\('(agent-[a-z]+)',\s*'(v\d+)',\s*'((?:[^']|'')*)',\s*(true|false)\)", agent_block())
    return [(n, v, body.replace("''", "'"), active == "true") for n, v, body, active in rows]


def seed_files() -> dict[str, str]:
    return dict(re.findall(r"\('(demo-site/[^']+)', \$f\$(.*?)\$f\$\)", agent_block(), re.S))


def prompt_body(name: str, version: str) -> str:
    return next(b for n, v, b, _ in seed_prompts() if (n, v) == (name, version))


class MemoryAgentRepo:
    def __init__(self, *, seed: bool = True, down: bool = False, fail_on: set[str] | None = None):
        self.down = down
        self.fail_on = set(fail_on or ())
        self._clock = datetime(2026, 10, 17, tzinfo=timezone.utc)
        self.prompts: list[dict] = []
        self.activations: list[dict] = []
        self.files: list[dict] = []
        self.tasks: dict = {}
        self.calls: list[dict] = []
        if seed:
            self.prompts = [{"name": n, "version": v, "body": b, "active": a, "version_id": uuid.uuid4()}
                            for n, v, b, a in seed_prompts()]
            for path, content in seed_files().items():
                self.add_file_version(path, content)

    def _tick(self) -> datetime:
        self._clock += timedelta(seconds=1)
        return self._clock

    def _check(self, op: str) -> None:
        if self.down or op in self.fail_on:
            raise AgentDbUnavailable(op)

    def add_file_version(self, path: str, content: str) -> None:
        version = 1 + max((f["version"] for f in self.files if f["path"] == path), default=0)
        self.files.append({"path": path, "version": version, "content": content,
                           "sha256": sha256_hex(content), "created_at": self._tick()})

    # ---------- промпти ----------

    async def list_prompts(self):
        self._check("list_prompts")
        return [dict(r) for r in sorted(self.prompts, key=lambda r: (r["name"], r["version"]))]

    async def fetch_active_prompt(self, name):
        self._check("fetch_active_prompt")
        row = next((r for r in self.prompts if r["name"] == name and r["active"]), None)
        return None if row is None else (row["version"], row["body"], row["version_id"])

    def _switch(self, name, version, previous, actor, quote):
        for r in self.prompts:
            if r["name"] == name:
                r["active"] = r["version"] == version
        self.activations.append({"name": name, "version": version, "previous_version": previous,
                                 "actor": actor, "approval_quote": quote})

    async def activate_prompt(self, name, version, actor, quote):
        self._check("activate_prompt")
        mine = [r for r in self.prompts if r["name"] == name]
        if not any(r["version"] == version for r in mine):
            return None
        previous = next((r["version"] for r in mine if r["active"]), None)
        self._switch(name, version, previous, actor, quote)
        return version, previous

    async def rollback_prompt(self, name, actor, quote):
        self._check("rollback_prompt")
        mine = [r for r in self.prompts if r["name"] == name]
        history = [a for a in self.activations if a["name"] == name]
        target = history[-1]["previous_version"] if history else None
        if target is None or not any(r["version"] == target for r in mine):
            return None
        previous = next((r["version"] for r in mine if r["active"]), None)
        self._switch(name, target, previous, actor, quote)
        return target, previous

    # ---------- файли сайту ----------

    def _current(self) -> dict[str, dict]:
        cur: dict[str, dict] = {}
        for f in self.files:
            if f["path"] not in cur or f["version"] > cur[f["path"]]["version"]:
                cur[f["path"]] = f
        return cur

    async def list_site_files(self):
        self._check("list_site_files")
        return [{"path": f["path"], "version": f["version"], "sha256": f["sha256"],
                 "bytes": len(f["content"].encode("utf-8"))} for _, f in sorted(self._current().items())]

    async def get_site_file(self, path, version):
        self._check("get_site_file")
        if version is None:
            f = self._current().get(path)
        else:
            f = next((x for x in self.files if x["path"] == path and x["version"] == version), None)
        return None if f is None else {k: f[k] for k in ("path", "version", "sha256", "content")}

    async def site_files(self, paths, base):
        self._check("site_files")
        if base == "seed":
            pool = {f["path"]: f for f in self.files if f["version"] == 1}
        else:
            pool = self._current()
        return [{k: pool[p][k] for k in ("path", "version", "sha256", "content")} for p in paths if p in pool]

    # ---------- задачі й виклики ----------

    async def create_task(self, task_id, task_text, mode, base):
        self._check("create_task")
        now = self._tick()
        self.tasks[task_id] = {"task_id": task_id, "created_at": now, "updated_at": now, "task_text": task_text,
                               "intent": None, "mode": mode, "base": base, "status": "received", "message": None,
                               "prompt_version": None, "context_sha256": None, "base_versions": {},
                               "patch_diff": None, "patch_sha256": None, "gate_report": [],
                               "total_prompt_tokens": 0, "total_completion_tokens": 0, "error": None,
                               "complexity": None, "total_cost_usd": None}

    async def insert_call(self, task_id, seq, step, provider, model, prompt_name, prompt_version, prompt_sha256,
                          prompt_tokens, completion_tokens, latency_ms, status, cost_usd):
        self._check("insert_call")
        assert cost_usd is None or (isinstance(cost_usd, Decimal) and cost_usd == cost_usd.quantize(Decimal("0.000001")))
        assert task_id in self.tasks                                    # REFERENCES agent_tasks
        assert not any(c["task_id"] == task_id and c["seq"] == seq for c in self.calls)   # UNIQUE (task_id, seq)
        assert step in ("plan", "propose")                              # CHECK step
        self.calls.append({"task_id": task_id, "seq": seq, "step": step, "provider": provider, "model": model,
                           "prompt_name": prompt_name, "prompt_version": prompt_version,
                           "prompt_sha256": prompt_sha256, "prompt_tokens": prompt_tokens,
                           "completion_tokens": completion_tokens, "latency_ms": latency_ms, "status": status,
                           "cost_usd": cost_usd, "created_at": self._tick()})

    async def finish_task(self, task_id, *, status, message, intent, prompt_version, context_sha256,
                          base_versions, patch_diff, patch_sha256, gate_report, prompt_tokens,
                          completion_tokens, error, complexity, total_cost_usd):
        self._check("finish_task")
        self.tasks[task_id].update({
            "status": status, "message": message, "intent": intent, "prompt_version": prompt_version,
            "context_sha256": context_sha256, "base_versions": dict(base_versions), "patch_diff": patch_diff,
            "patch_sha256": patch_sha256, "gate_report": list(gate_report), "total_prompt_tokens": prompt_tokens,
            "total_completion_tokens": completion_tokens, "error": error, "complexity": complexity,
            "total_cost_usd": total_cost_usd, "updated_at": self._tick()})

    async def cost_today(self):
        # у пам'яті всі виклики «сьогоднішні»; SUM ігнорує NULL, порожня сума — 0 (COALESCE)
        self._check("cost_today")
        def total(calls):
            return sum((c["cost_usd"] for c in calls if c["cost_usd"] is not None), Decimal("0"))
        by_model = {}
        for c in self.calls:
            by_model.setdefault(c["model"], []).append(c)
        rows = [{"model": m, "calls": len(cs), "cost_usd": total(cs)} for m, cs in by_model.items()]
        rows.sort(key=lambda r: (-r["cost_usd"], r["model"]))
        real = [c for c in self.calls if self.tasks[c["task_id"]]["mode"] == "real"]
        return {"today_usd": total(self.calls), "real_today_usd": total(real), "by_model": rows}

    def calls_of(self, task_id) -> list[dict]:
        return sorted((c for c in self.calls if c["task_id"] == task_id), key=lambda c: c["seq"])

    async def list_tasks(self, limit):
        self._check("list_tasks")
        rows = sorted(self.tasks.values(), key=lambda t: t["created_at"], reverse=True)[:limit]
        out = []
        for t in rows:
            calls = self.calls_of(t["task_id"])
            out.append({"task_id": t["task_id"], "created_at": t["created_at"], "status": t["status"],
                        "intent": t["intent"], "total_cost_usd": t["total_cost_usd"],
                        "model": calls[-1]["model"] if calls else None})
        return out

    async def get_task(self, task_id):
        self._check("get_task")
        t = self.tasks.get(task_id)
        if t is None:
            return None
        task = dict(t)
        task["calls"] = [{k: v for k, v in c.items() if k not in ("task_id", "created_at")}
                         for c in self.calls_of(task_id)]
        return task
