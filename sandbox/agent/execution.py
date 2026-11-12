"""Local durable step journal. Never automatically retry an uncertain send.

The lock spans only disk transactions, never provider I/O. Unknown real calls
retain their reservation until an operator reconciles them. This is a local
prototype, not a distributed transaction with the provider or PostgreSQL.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone


class JournalError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def envelope(mode, step, model, prompt, messages, max_tokens, policy):
    return {"v": 1, "mode": mode, "step": step, "model": model,
            "prompt": {"name": prompt.name, "version": prompt.version, "sha256": prompt.sha256},
            "messages_sha256": digest(messages), "max_tokens": max_tokens,
            "policy_sha256": digest(policy)}


class Journal:
    def __init__(self, root=None):
        self.root = Path(root or os.getenv("AGENT_EXECUTION_DIR", "data/agent-executions"))

    @contextlib.contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".lock").open("a+b") as f:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                f.write(b"0")
                f.flush()
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f, fcntl.LOCK_UN)

    def _path(self, run_id, step_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(run_id)) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(step_id)):
            raise JournalError("invalid execution identity")
        return self.root / f"{run_id}--{step_id}.json"

    def _read(self, path):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            checksum = record.pop("checksum")
            if digest(record) != checksum:
                raise ValueError("checksum")
            return record
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise JournalError("corrupt execution journal") from exc

    def _write(self, path, record):
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        body = {**record, "checksum": digest(record)}
        fd, name = tempfile.mkstemp(prefix=".write-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(body, f, ensure_ascii=False, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, path)
            if os.name != "nt":
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def has_step(self, run_id, step_id):
        """Identity existence only. prepare still validates checksum and envelope."""
        return self._path(run_id, step_id).exists()

    def prepare(self, run_id, step_id, frozen, safe_messages, estimate=0, spent=0, budget=None):
        """Atomically reserve and freeze before send. Bodies may be omitted by gate."""
        path = self._path(run_id, step_id)
        with self._lock():
            if path.exists():
                old = self._read(path)
                if old["envelope_sha256"] != digest(frozen):
                    raise JournalError("execution envelope mismatch")
                if old["state"] in ("completed", "accounted"):
                    if old.get("result") is None:
                        raise JournalError("result body omitted; replay unavailable")
                    return old
                raise JournalError("uncertain execution; operator reconciliation required")
            held = sum(float(self._read(p).get("reservation_usd", 0)) for p in self.root.glob("*.json"))
            if budget is not None and float(spent) + held + float(estimate) > float(budget):
                raise JournalError("budget reservations exceeded")
            record = {"run_id": str(run_id), "step_id": str(step_id), "state": "prepared",
                      "envelope": frozen, "envelope_sha256": digest(frozen),
                      "messages": safe_messages, "reservation_usd": float(estimate), "result": None,
                      "transitions": ["prepared"]}
            self._write(path, record)
            return record

    def transition(self, run_id, step_id, state, result=None):
        path = self._path(run_id, step_id)
        allowed = {"prepared": {"pending"}, "pending": {"completed", "unknown"},
                   "completed": {"accounted"}, "unknown": set(), "accounted": set()}
        with self._lock():
            record = self._read(path)
            if state not in allowed[record["state"]]:
                raise JournalError("invalid execution transition")
            record["state"] = state
            record["transitions"].append(state)
            if state == "completed":
                record["result"] = result
            if state == "accounted":
                record["reservation_usd"] = 0
            self._write(path, record)

    def inspect(self, run_id=None):
        if run_id is not None:
            self._path(run_id, "validate")
        if not self.root.exists():
            return []
        with self._lock():
            records = [self._read(p) for p in sorted(self.root.glob("*.json"))]
        safe = []
        for r in records:
            if run_id is not None and r["run_id"] != str(run_id):
                continue
            safe.append({"run_id": r["run_id"], "step_id": r["step_id"],
                         "state": r["state"], "envelope_sha256": r["envelope_sha256"],
                         "model": r["envelope"]["model"], "mode": r["envelope"]["mode"],
                         "reservation_usd": r["reservation_usd"], "updated_at": r["updated_at"],
                         "result_saved": r.get("result") is not None})
        return safe

    def reconcile(self, run_id, step_id, *, note, confirmed_cost_usd):
        """Explicit local operator decision; never invokes or retries the provider.

        Caller must first record confirmed cost in its billing ledger. This
        method only releases a held reservation and preserves the audit reason.
        """
        if not note.strip() or confirmed_cost_usd < 0:
            raise JournalError("reconciliation needs reason and confirmed cost")
        with self._lock():
            path = self._path(run_id, step_id)
            record = self._read(path)
            if record["state"] not in ("prepared", "pending", "unknown"):
                raise JournalError("execution is not uncertain")
            record["state"] = "reconciled"
            record["transitions"].append("reconciled")
            # Persist only reason digest; operator notes can contain private data.
            record["reconciliation"] = {"note_sha256": digest(note), "confirmed_cost_usd": confirmed_cost_usd}
            record["reservation_usd"] = 0
            self._write(path, record)
