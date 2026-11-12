"""Агентський репозиторій у пам'яті з тією ж семантикою, що AgentRepo над Postgres (тести без БД).

Seed читається з агентського блоку db/schema.sql: тести бачать ті самі промпти й файли demo-site,
що й живий том, тож очікування (ханки diff, sha) рахуються на справжньому seed.
"""

import pathlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sandbox.agent.repo import AgentDbUnavailable, next_version_number, cost_summary, chat_action_projection
from sandbox.core.hashing import sha256_hex

SCHEMA = pathlib.Path(__file__).resolve().parents[2] / "db" / "schema.sql"
BEGIN = "-- === hw1: agent (початок) ==="
END = "-- === hw1: agent (кінець) ==="


def agent_block() -> str:
    text = SCHEMA.read_text(encoding="utf-8")
    return text[text.index(BEGIN):text.index(END) + len(END)]


def seed_prompts() -> list[tuple[str, str, str, bool]]:
    # промпти — з усіх агентських блоків: agent-chat додає міграція hw5
    text = SCHEMA.read_text(encoding="utf-8")
    rows = re.findall(r"\('(agent-[a-z]+)',\s*'(v\d+)',\s*'((?:[^']|'')*)',\s*(true|false)\)", text[text.index(BEGIN):])
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
        self.instructions: list[dict] = []
        self.instruction_active: dict = {}
        self.approvals: dict = {}
        self.chat_turns: dict = {}
        self.chat_calls: list[dict] = []
        self.snapshots: dict = {}
        self.context_runs: dict = {}
        self.workspace_calls: list[dict] = []
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

    def add_file_version(self, path: str, content: str, source: str = "seed", task_id=None, approval_id=None) -> None:
        version = 1 + max((f["version"] for f in self.files if f["path"] == path), default=0)
        self.files.append({"path": path, "version": version, "content": content, "sha256": sha256_hex(content),
                           "source": source, "source_task_id": task_id, "approval_id": approval_id,
                           "created_at": self._tick()})

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

    async def get_prompt(self, name, version):
        self._check("get_prompt")
        r = next((r for r in self.prompts if (r["name"], r["version"]) == (name, version)), None)
        return None if r is None else {**r, "created_at": r.get("created_at") or self._clock}

    async def create_prompt_version(self, name, body):
        self._check("create_prompt_version")
        mine = [r for r in self.prompts if r["name"] == name]
        if not mine:
            return None
        same = next((r["version"] for r in mine if r["body"] == body), None)
        if same is not None:
            return same, False
        version = f"v{next_version_number([r['version'] for r in mine])}"
        self.prompts.append({"name": name, "version": version, "body": body, "active": False,
                             "version_id": uuid.uuid4(), "created_at": self._tick()})
        return version, True

    async def list_activations(self, name, limit):
        self._check("list_activations")
        mine = [a for a in self.activations if a["name"] == name]
        # час активації в пам'яті не зберігаємо (рядок аудиту лишається тим самим, що й у тестах hw1)
        return [{**a, "activated_at": self._clock} for a in reversed(mine)][:limit]

    # ---------- реєстр інструкцій (I-1) ----------

    async def import_instruction(self, kind, name, body, sha256, source, source_path, source_ref):
        self._check("import_instruction")
        mine = [r for r in self.instructions if (r["kind"], r["name"]) == (kind, name)]
        same = next((r["version"] for r in mine if r["sha256"] == sha256), None)
        if same is not None:
            return same, False
        version = f"v{next_version_number([r['version'] for r in mine])}"
        self.instructions.append({"kind": kind, "name": name, "version": version, "sha256": sha256, "body": body,
                                  "source": source, "source_path": source_path, "source_ref": source_ref,
                                  "imported_at": self._tick()})
        return version, True

    async def list_instructions(self, project):
        self._check("list_instructions")
        out = {}
        for r in self.instructions:
            out.setdefault((r["kind"], r["name"]), []).append(r)
        rows = []
        for (kind, name), vers in sorted(out.items()):
            last = max(vers, key=lambda r: r["imported_at"])
            act = self.instruction_active.get((project, kind, name))
            rows.append({"kind": kind, "name": name, "versions": len(vers), "latest": last["version"],
                         "source": last["source"], "source_path": last["source_path"],
                         "active_version": act and act["version"], "share_with_model": act and act["share"]})
        return rows

    async def instruction_versions(self, kind, name):
        self._check("instruction_versions")
        return [dict(r) for r in self.instructions if (r["kind"], r["name"]) == (kind, name)]

    async def get_instruction(self, kind, name, version):
        self._check("get_instruction")
        return next((dict(r) for r in self.instructions if (r["kind"], r["name"], r["version"]) == (kind, name, version)),
                    None)

    async def activate_instruction(self, project, kind, name, version):
        self._check("activate_instruction")
        if not any((r["kind"], r["name"], r["version"]) == (kind, name, version) for r in self.instructions):
            return False
        prev = self.instruction_active.get((project, kind, name))
        self.instruction_active[(project, kind, name)] = {"version": version, "share": prev["share"] if prev else False}
        return prev and prev["version"]

    async def set_instruction_share(self, project, kind, name, share):
        self._check("set_instruction_share")
        act = self.instruction_active.get((project, kind, name))
        if act is None:
            return False
        act["share"] = share
        return True

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
                               "complexity": None, "total_cost_usd": None, "approval_id": None}

    async def insert_call(self, task_id, seq, step, provider, model, prompt_name, prompt_version, prompt_sha256,
                          prompt_tokens, completion_tokens, latency_ms, status, cost_usd, cache_hit=False):
        self._check("insert_call")
        assert cost_usd is None or (isinstance(cost_usd, Decimal) and cost_usd == cost_usd.quantize(Decimal("0.000001")))
        assert task_id in self.tasks                                    # REFERENCES agent_tasks
        assert not any(c["task_id"] == task_id and c["seq"] == seq for c in self.calls)   # UNIQUE (task_id, seq)
        assert step in ("plan", "propose")                              # CHECK step
        self.calls.append({"task_id": task_id, "seq": seq, "step": step, "provider": provider, "model": model,
                           "prompt_name": prompt_name, "prompt_version": prompt_version,
                           "prompt_sha256": prompt_sha256, "prompt_tokens": prompt_tokens,
                           "completion_tokens": completion_tokens, "latency_ms": latency_ms, "status": status,
                           "cost_usd": cost_usd, "cache_hit": cache_hit, "created_at": self._tick()})

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
        rows = ([dict(c, mode=self.tasks[c["task_id"]]["mode"]) for c in self.calls]
                + [dict(c, mode=self.chat_turns[c["turn_id"]]["mode"]) for c in self.chat_calls]
                + [dict(c, mode=self.context_runs[c["run_id"]]["mode"]) for c in self.workspace_calls])
        summary = cost_summary(rows)
        models = [{"model": m, **cost_summary(c for c in rows if c["model"] == m)}
                  for m in sorted({c["model"] for c in rows})]
        for row in models:
            row["calls"] = row["records"]
        models.sort(key=lambda r: (-r["cost_usd"], r["model"]))
        return {**summary, "today_usd": summary["cost_usd"], "real_today_usd": summary["real_estimate_usd"],
                "synthetic_today_usd": summary["synthetic_usd"], "by_model": models}

    # ---------- заявки (W4) ----------

    async def create_approval(self, approval_id, task_id, key, base_versions, diff, patch_sha256, files,
                              action="apply_patch"):
        self._check("create_approval")
        a = next((x for x in self.approvals.values() if x["idempotency_key"] == key and x["status"] == "pending"), None)
        if a is None:
            a = {"approval_id": approval_id, "task_id": task_id, "created_at": self._tick(), "decided_at": None,
                 "status": "pending", "action": action, "idempotency_key": key,
                 "base_versions": dict(base_versions), "patch_diff": diff, "patch_sha256": patch_sha256,
                 "files": list(files), "reason": None, "conflicts": []}
            self.approvals[approval_id] = a
        self.tasks[task_id]["approval_id"] = a["approval_id"]
        return dict(a)

    async def get_approval(self, approval_id):
        self._check("get_approval")
        a = self.approvals.get(approval_id)
        return None if a is None else dict(a)

    async def list_approvals(self, status, limit):
        self._check("list_approvals")
        rows = [a for a in self.approvals.values() if status is None or a["status"] == status]
        return [dict(a) for a in sorted(rows, key=lambda a: a["created_at"], reverse=True)[:limit]]

    async def close_approval(self, approval_id, status, reason, conflicts):
        self._check("close_approval")
        a = self.approvals.get(approval_id)
        if a is None or a["status"] != "pending":
            return False
        a.update(status=status, reason=reason, conflicts=list(conflicts), decided_at=self._tick())
        return True

    async def apply_approval(self, approval_id, task_id, new_files):
        self._check("apply_approval")
        a = self.approvals.get(approval_id)
        if a is None or a["status"] != "pending":
            return "gone"
        cur = self._current()
        if any(cur[f["path"]]["version"] + 1 != f["version"] for f in new_files):   # PK (path, version)
            await self.close_approval(approval_id, "stale", "base changed while applying", [])
            return "stale"
        a.update(status="applied", decided_at=self._tick())
        for f in new_files:
            self.add_file_version(f["path"], f["content"], "approval", task_id, approval_id)
        return "applied"

    async def reset_site(self, seed):
        self._check("reset_site")
        changed = []
        for f in seed:
            cur = self._current().get(f["path"])
            if cur is not None and cur["sha256"] != f["sha256"]:
                self.add_file_version(f["path"], f["content"], "reset")
                changed.append(f["path"])
        return changed

    async def site_history(self, limit):
        self._check("site_history")
        rows = sorted(self.files, key=lambda f: (f["created_at"], f["path"]), reverse=True)[:limit]
        return [{k: f[k] for k in ("path", "version", "sha256", "source", "source_task_id", "approval_id", "created_at")}
                for f in rows]

    def calls_of(self, task_id) -> list[dict]:
        return sorted((c for c in self.calls if c["task_id"] == task_id), key=lambda c: c["seq"])

    async def list_tasks(self, limit):
        self._check("list_tasks")
        rows = sorted(self.tasks.values(), key=lambda t: t["created_at"], reverse=True)[:limit]
        out = []
        for t in rows:
            calls = self.calls_of(t["task_id"])
            out.append({"task_id": t["task_id"], "created_at": t["created_at"], "status": t["status"],
                        "intent": t["intent"], "mode": t.get("mode"), "total_cost_usd": t["total_cost_usd"],
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

    # ---------- чат (W5) ----------

    async def create_chat_turn(self, turn_id, chat_id, seq, mode, user_text, case_id):
        self._check("create_chat_turn")
        assert not any(t["chat_id"] == chat_id and t["seq"] == seq for t in self.chat_turns.values())  # UNIQUE
        assert mode in ("mock", "real")
        now = self._tick()
        self.chat_turns[turn_id] = {"turn_id": turn_id, "chat_id": chat_id, "seq": seq, "created_at": now,
                                    "updated_at": now, "mode": mode, "user_text": user_text, "status": "received",
                                    "reply": None, "proposal_text": None, "task_id": None, "case_id": case_id,
                                    "gate_report": [], "total_prompt_tokens": 0, "total_completion_tokens": 0,
                                    "total_cost_usd": None, "error": None}

    async def insert_chat_call(self, turn_id, seq, step, provider, model, prompt_name, prompt_version, prompt_sha256,
                               prompt_tokens, completion_tokens, latency_ms, status, cost_usd, cache_hit=False):
        self._check("insert_chat_call")
        assert turn_id in self.chat_turns and step == "chat"                 # REFERENCES, CHECK step
        assert not any(c["turn_id"] == turn_id and c["seq"] == seq for c in self.chat_calls)
        self.chat_calls.append({"turn_id": turn_id, "seq": seq, "step": step, "provider": provider, "model": model,
                                "prompt_name": prompt_name, "prompt_version": prompt_version,
                                "prompt_sha256": prompt_sha256, "prompt_tokens": prompt_tokens,
                                "completion_tokens": completion_tokens, "latency_ms": latency_ms, "status": status,
                                "cost_usd": cost_usd, "cache_hit": cache_hit, "created_at": self._tick()})

    async def finish_chat_turn(self, turn_id, *, status, reply, proposal_text, task_id, gate_report,
                               prompt_tokens, completion_tokens, total_cost_usd, error):
        self._check("finish_chat_turn")
        assert task_id is None or uuid.UUID(str(task_id)) in self.tasks          # REFERENCES agent_tasks
        self.chat_turns[turn_id].update(status=status, reply=reply, proposal_text=proposal_text,
                                        task_id=None if task_id is None else uuid.UUID(str(task_id)),
                                        gate_report=list(gate_report), total_prompt_tokens=prompt_tokens,
                                        total_completion_tokens=completion_tokens, total_cost_usd=total_cost_usd,
                                        error=error, updated_at=self._tick())

    async def chat_history(self, chat_id, limit):
        self._check("chat_history")
        rows = sorted((t for t in self.chat_turns.values() if t["chat_id"] == chat_id), key=lambda t: t["seq"])[-limit:]
        return [{k: t[k] for k in ("seq", "user_text", "reply", "status")} for t in rows]

    async def get_chat(self, chat_id):
        self._check("get_chat")
        out = []
        for t in sorted((t for t in self.chat_turns.values() if t["chat_id"] == chat_id), key=lambda t: t["seq"]):
            task = self.tasks.get(t["task_id"]) if t["task_id"] else None
            a = self.approvals.get(task["approval_id"]) if task and task["approval_id"] else None
            out.append({**{k: v for k, v in t.items() if k not in ("updated_at", "total_prompt_tokens",
                                                                  "total_completion_tokens")},
                        "approval_id": a and a["approval_id"], "approval_status": a and a["status"]})
        for row in out:
            row.update(chat_action_projection(row, [c for c in self.chat_calls if c["turn_id"] == row["turn_id"]],
                                              [c for c in self.calls if c["task_id"] == row["task_id"]]))
        return out

    async def list_chats(self, limit):
        self._check("list_chats")
        chats = {}
        for t in sorted(self.chat_turns.values(), key=lambda t: t["seq"]):
            chats.setdefault(t["chat_id"], []).append(t)
        rows = [{"chat_id": cid, "started_at": ts[0]["created_at"], "last_at": max(x["created_at"] for x in ts),
                 "turns": len(ts), "first_text": ts[0]["user_text"], "case_id": ts[0]["case_id"],
                 "cost_usd": sum((x["total_cost_usd"] or Decimal("0") for x in ts), Decimal("0"))}
                for cid, ts in chats.items()]
        for row in rows:
            turns = await self.get_chat(row["chat_id"])
            row["action_cost_usd"] = sum(t["action_cost_usd"] for t in turns)
            for key in ("real_estimate_usd", "synthetic_usd", "unknown_cost_calls"):
                row[key] = sum(t["accounting"][key] for t in turns)
        return sorted(rows, key=lambda r: r["last_at"], reverse=True)[:limit]

    # ---------- робочі простори (W6) ----------

    async def save_workspace_snapshot(self, project, snapshot_sha256, files):
        self._check("save_workspace_snapshot")
        self.snapshots.setdefault((project, snapshot_sha256), list(files))          # ON CONFLICT DO NOTHING

    async def create_context_run(self, run_id, project, snapshot_sha256, agent, mode, question, budget_tokens, pack,
                                 package, pack_sha256, package_sha256):
        self._check("create_context_run")
        assert (project, snapshot_sha256) in self.snapshots and mode in ("mock", "real")   # FK, CHECK
        assert run_id not in self.context_runs
        assert all("text" not in i for i in pack["items"]) and "messages" not in package  # тексти в БД не пишемо
        now = self._tick()
        self.context_runs[run_id] = {"run_id": run_id, "created_at": now, "updated_at": now, "project": project,
                                     "snapshot_sha256": snapshot_sha256, "agent": agent, "mode": mode,
                                     "question": question, "budget_tokens": budget_tokens, "pack": pack,
                                     "package": package, "pack_sha256": pack_sha256, "package_sha256": package_sha256,
                                     "status": "received", "answer": None, "citations": [], "missing": [],
                                     "gate_report": [], "total_prompt_tokens": 0, "total_completion_tokens": 0,
                                     "total_cost_usd": None, "error": None}

    async def insert_workspace_call(self, run_id, seq, step, provider, model, prompt_name, prompt_version,
                                    prompt_sha256, prompt_tokens, completion_tokens, latency_ms, status, cost_usd,
                                    cache_hit=False):
        self._check("insert_workspace_call")
        assert run_id in self.context_runs and step == "explore"                 # REFERENCES, CHECK step
        assert not any(c["run_id"] == run_id and c["seq"] == seq for c in self.workspace_calls)
        self.workspace_calls.append({"run_id": run_id, "seq": seq, "step": step, "provider": provider, "model": model,
                                     "prompt_name": prompt_name, "prompt_version": prompt_version,
                                     "prompt_sha256": prompt_sha256, "prompt_tokens": prompt_tokens,
                                     "completion_tokens": completion_tokens, "latency_ms": latency_ms,
                                     "status": status, "cost_usd": cost_usd, "cache_hit": cache_hit,
                                     "created_at": self._tick()})

    async def finish_context_run(self, run_id, *, status, answer, citations, missing, gate_report, prompt_tokens,
                                 completion_tokens, total_cost_usd, error):
        self._check("finish_context_run")
        self.context_runs[run_id].update(status=status, answer=answer, citations=list(citations),
                                         missing=list(missing), gate_report=list(gate_report),
                                         total_prompt_tokens=prompt_tokens, total_completion_tokens=completion_tokens,
                                         total_cost_usd=total_cost_usd, error=error, updated_at=self._tick())

    async def list_context_runs(self, limit):
        self._check("list_context_runs")
        rows = sorted(self.context_runs.values(), key=lambda r: r["created_at"], reverse=True)[:limit]
        return [{**{k: r[k] for k in ("run_id", "created_at", "project", "mode", "question", "status", "budget_tokens",
                                      "pack_sha256", "package_sha256", "total_cost_usd")},
                 "used_tokens": r["pack"]["used_tokens"], "chunks": len(r["pack"]["items"])} for r in rows]

    async def get_context_run(self, run_id):
        self._check("get_context_run")
        r = self.context_runs.get(run_id)
        if r is None:
            return None
        calls = [{k: v for k, v in c.items() if k not in ("run_id", "prompt_sha256", "created_at")}
                 for c in sorted(self.workspace_calls, key=lambda c: c["seq"]) if c["run_id"] == run_id]
        return {**r, "calls": calls}
