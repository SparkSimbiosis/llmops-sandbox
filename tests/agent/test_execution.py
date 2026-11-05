from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from sandbox.agent.execution import Journal, JournalError, digest, envelope


def frozen(**changes):
    args = dict(mode="real", step="plan", model="fast", prompt=SimpleNamespace(name="p", version=1, sha256="abc"),
                messages=[{"role": "user", "content": "hello"}], max_tokens=100, policy={"budget": 1})
    args.update(changes)
    return envelope(**args)


def test_envelope_identity():
    assert digest(frozen()) == digest(frozen())
    assert digest(frozen()) != digest(frozen(model="other"))
    assert digest(frozen()) != digest(frozen(policy={"budget": 2}))
    assert digest(frozen()) != digest(frozen(messages=[{"role": "system", "content": "hello"}]))


def test_write_before_send_and_replay(tmp_path):
    j = Journal(tmp_path)
    assert j.prepare("run", "1", frozen(), None)["state"] == "prepared"
    j.transition("run", "1", "pending")
    with pytest.raises(JournalError, match="uncertain"):
        j.prepare("run", "1", frozen(), None)
    j.transition("run", "1", "completed", {"content": "saved"})
    assert j.prepare("run", "1", frozen(), None)["result"]["content"] == "saved"
    with pytest.raises(JournalError, match="mismatch"):
        j.prepare("run", "1", frozen(model="new"), None)


def test_unknown_holds_budget_until_explicit_reconciliation(tmp_path):
    j = Journal(tmp_path)
    j.prepare("r", "1", frozen(), None, estimate=.6, budget=1)
    j.transition("r", "1", "pending")
    j.transition("r", "1", "unknown")
    with pytest.raises(JournalError, match="budget"):
        j.prepare("r", "2", frozen(), None, estimate=.6, budget=1)
    j.reconcile("r", "1", note="operator verified no charge", confirmed_cost_usd=0)
    j.prepare("r", "2", frozen(), None, estimate=.6, budget=1)


def test_concurrent_reservations_are_atomic(tmp_path):
    def reserve(i):
        try:
            Journal(tmp_path).prepare("r", str(i), frozen(), None, estimate=.6, budget=1)
            return True
        except JournalError:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, range(4))) == 1


def test_corruption_and_traversal_fail_closed(tmp_path):
    j = Journal(tmp_path)
    j.prepare("r", "1", frozen(), None)
    (tmp_path / "r--1.json").write_text('{}')
    with pytest.raises(JournalError, match="corrupt"):
        j.inspect()
    with pytest.raises(JournalError, match="identity"):
        j.inspect("../outside")


def test_disk_failure_before_prepare_leaves_no_send_permission(tmp_path, monkeypatch):
    j = Journal(tmp_path)
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(j, "_write", fail)
    with pytest.raises(OSError):
        j.prepare("r", "1", frozen(), None)
    assert j.inspect() == []


@pytest.mark.asyncio
async def test_pipeline_completed_replay_and_changed_identity(mem, monkeypatch, tmp_path):
    from sandbox.agent import pipeline
    from sandbox.llm.types import LLMResult
    count = 0
    async def complete(*args, **kwargs):
        nonlocal count
        count += 1
        return LLMResult(ok=True, status=200, content="saved", provider="fake", model_alias="fast")
    monkeypatch.setattr(pipeline.llm, "complete", complete)
    prompt = SimpleNamespace(name="p", version=1, sha256="abc")
    # Use insertion stub: replay must not insert an already-accounted DB row.
    rows = []
    async def insert(*args, **kwargs):
        rows.append(args)
    run = pipeline._Run("same-run", "mock", False, insert=insert)
    await run.call("plan", prompt, [{"role": "user", "content": "hello"}], "fast")
    second = pipeline._Run("same-run", "mock", False, insert=insert)
    result = await second.call("plan", prompt, [{"role": "user", "content": "hello"}], "fast")
    assert result.content == "saved" and count == 1 and len(rows) == 1
    with pytest.raises(JournalError, match="mismatch"):
        await pipeline._Run("same-run", "mock", False, insert=insert).call(
            "plan", prompt, [{"role": "user", "content": "changed"}], "fast")


@pytest.mark.asyncio
async def test_provider_failure_is_unknown_and_never_retried(mem, monkeypatch):
    from sandbox.agent import pipeline
    count = 0
    async def broken(*args, **kwargs):
        nonlocal count
        count += 1
        raise RuntimeError("provider interrupted")
    monkeypatch.setattr(pipeline.llm, "complete", broken)
    prompt = SimpleNamespace(name="p", version=1, sha256="abc")
    with pytest.raises(RuntimeError):
        await pipeline._Run("failed", "mock", False).call("plan", prompt, [], "fast")
    assert Journal().inspect("failed")[0]["state"] == "unknown"
    with pytest.raises(JournalError, match="uncertain"):
        await pipeline._Run("failed", "mock", False).call("plan", prompt, [], "fast")
    assert count == 1


@pytest.mark.asyncio
async def test_database_log_failure_keeps_completed_response(mem, monkeypatch):
    from sandbox.agent import pipeline
    from sandbox.agent.repo import AgentDbUnavailable
    from sandbox.llm.types import LLMResult
    async def complete(*args, **kwargs):
        return LLMResult(ok=True, status=200, content="saved", provider="fake", model_alias="fast")
    async def insert(*args, **kwargs):
        raise AgentDbUnavailable("test logging failure")
    monkeypatch.setattr(pipeline.llm, "complete", complete)
    with pytest.raises(AgentDbUnavailable):
        await pipeline._Run("db-failed", "mock", False, insert=insert).call(
            "plan", SimpleNamespace(name="p", version=1, sha256="abc"), [], "fast")
    assert Journal().inspect("db-failed")[0]["state"] == "completed"


@pytest.mark.asyncio
async def test_profile_token_limit_and_snapshot_are_frozen(mem, monkeypatch):
    from sandbox.agent import pipeline
    from sandbox.llm.types import LLMResult
    limits = []
    async def complete(*args, **kwargs):
        limits.append(kwargs["max_tokens"])
        return LLMResult(ok=True, status=200, content="safe", provider="fake", model_alias="fast")
    async def insert(*args, **kwargs):
        pass
    monkeypatch.setattr(pipeline.llm, "complete", complete)
    snapshot = {"version": 1}
    run = pipeline._Run("profile", "mock", False, insert=insert, max_tokens=600,
                        execution_profile="quick", rules_snapshot=snapshot)
    snapshot["version"] = 2
    assert run.rules_snapshot == {"version": 1}
    await run.call("plan", SimpleNamespace(name="p", version=1, sha256="abc"), [], "fast")
    assert limits == [600]


@pytest.mark.asyncio
async def test_secret_like_response_is_omitted_from_journal(mem, monkeypatch):
    from sandbox.agent import pipeline
    from sandbox.llm.types import LLMResult
    marker = "sk-" + "z" * 25  # synthetic sentinel, not a credential
    async def complete(*args, **kwargs):
        return LLMResult(ok=True, status=200, content=marker, provider="fake", model_alias="fast")
    async def insert(*args, **kwargs):
        pass
    monkeypatch.setattr(pipeline.llm, "complete", complete)
    await pipeline._Run("redacted", "mock", False, insert=insert).call(
        "plan", SimpleNamespace(name="p", version=1, sha256="abc"), [], "fast")
    j = Journal()
    assert marker not in (j.root / "redacted--1-plan.json").read_text()
    assert not j.inspect("redacted")[0]["result_saved"]

@pytest.mark.asyncio
async def test_budget_snapshot_and_accounting_do_not_race(mem, monkeypatch):
    import asyncio
    from decimal import Decimal
    from sandbox.agent import pipeline
    from sandbox.llm.types import LLMResult
    spent = Decimal("0")
    sends = []
    async def cost_today():
        snapshot = spent
        await asyncio.sleep(0)
        return {"real_today_usd": snapshot}
    async def guard(*args):
        return None
    async def complete(*args, **kwargs):
        sends.append(1)
        await asyncio.sleep(0)
        return LLMResult(ok=True, status=200, content="safe", provider="gateway", model_alias="model")
    async def insert(*args, **kwargs):
        nonlocal spent
        spent += Decimal("0.006")
    monkeypatch.setattr(mem, "cost_today", cost_today)
    monkeypatch.setattr(pipeline._Run, "_guard", guard)
    monkeypatch.setattr(pipeline.llm, "complete", complete)
    monkeypatch.setattr(pipeline.pricing, "estimate_usd", lambda *args: Decimal("0.006"))
    monkeypatch.setattr(pipeline.pricing, "cost_usd", lambda *args: Decimal("0.006"))
    monkeypatch.setattr(pipeline, "real_budget_usd", .01)
    prompt = SimpleNamespace(name="p", version=1, sha256="abc")
    a = pipeline._Run("race-a", "real", False, insert=insert)
    b = pipeline._Run("race-b", "real", False, insert=insert)
    await asyncio.gather(a.call("plan", prompt, [], "model"), b.call("plan", prompt, [], "model"))
    assert len(sends) == 1
    assert spent == Decimal("0.006")
    assert a.blocked or b.blocked

@pytest.mark.asyncio
async def test_disk_failure_after_sql_commit_replays_without_provider(mem, monkeypatch):
    from sandbox.agent import pipeline
    from sandbox.llm.types import LLMResult
    rows, sends = {}, []
    async def complete(*args, **kwargs):
        sends.append(1)
        return LLMResult(ok=True, status=200, content="safe", provider="fake", model_alias="fast")
    async def idempotent_insert(run_id, seq, *args, **kwargs):
        rows.setdefault((run_id, seq), args)
    original = Journal.transition
    failed = False
    def transition(self, run, step, state, *args, **kwargs):
        nonlocal failed
        if state == "accounted" and not failed:
            failed = True
            raise OSError("disk interruption after SQL commit")
        return original(self, run, step, state, *args, **kwargs)
    monkeypatch.setattr(Journal, "transition", transition)
    monkeypatch.setattr(pipeline.llm, "complete", complete)
    prompt = SimpleNamespace(name="p", version=1, sha256="abc")
    with pytest.raises(OSError):
        await pipeline._Run("sql-gap", "mock", False, insert=idempotent_insert).call("plan", prompt, [], "fast")
    assert Journal().inspect("sql-gap")[0]["state"] == "completed"
    await pipeline._Run("sql-gap", "mock", False, insert=idempotent_insert).call("plan", prompt, [], "fast")
    assert len(rows) == 1 and len(sends) == 1
    assert Journal().inspect("sql-gap")[0]["state"] == "accounted"
