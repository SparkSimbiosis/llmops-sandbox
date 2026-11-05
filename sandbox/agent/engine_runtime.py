"""Prepared Engine execution with explicit roles, output contracts and storage lineage.

All Engine calls are read/propose-only. The existing workspace call table stores
step='explore' for schema compatibility; packet/journal keep engine:<engine_id>.
"""
import asyncio
from copy import deepcopy
import json
import re
import time
from uuid import uuid4

from sandbox.agent import builder, chat, configs, context, contextpack, enginefake, gates, outgoing, passports, pipeline, pricing, routing, workspace
from sandbox.agent.execution import JournalError
from sandbox.agent.prompts import AgentPrompt
from sandbox.agent.repo import AgentDbUnavailable
from sandbox.agent.settings import AGENT_PROVIDER_MODE, ALLOWED_PATHS, resolve_mode
from sandbox.core.hashing import canonical_json, sha256_hex
from sandbox.llm.fake_provider import tokens

PREPARED = {}
TTL_SECONDS = 1800
MAX_PREPARED = 50
LOCK = asyncio.Lock()

class EngineError(ValueError):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def _agent(engine):
    return next(aid for aid, a in configs.AGENTS.items() if a.get("engine") == engine)


def _signature(engine):
    aid = _agent(engine)
    a = configs.AGENTS[aid]
    return sha256_hex(canonical_json({"engine": builder.ENGINES[engine], "agent": a,
                "role": configs.ROLES[a["role"]], "policy": configs.POLICY_DOC,
                "skills": {s: configs.SKILLS[s] for s in a.get("skills", [])},
                "contract": builder.CONTRACTS[builder.ENGINES[engine]["contract"]],
                "outgoing_rules_sha256": sha256_hex(canonical_json([p.pattern for p in (*pipeline.gate_rules.secrets, *pipeline.gate_rules.denylist)]))}))


async def _source(project, question, budget, require_full=False):
    if project == "demo-site":
        files = await pipeline.repo.site_files(list(ALLOWED_PATHS), "current")
        meta = [{k: f[k] for k in ("path", "version", "sha256")} for f in files]
        snapshot = sha256_hex(canonical_json(meta))
        selected, used = [], 0
        for f in files:
            size = tokens(f["content"])
            if used + size <= budget:
                selected.append(f); used += size
        if require_full and len(selected) != len(files):
            raise EngineError("context_budget_exceeded_use_thorough", 422)
        data = context.propose_messages("", question, selected)[1]["content"]
        pack = {"items": [{"path": f["path"], "sha256": f["sha256"], "tokens": tokens(f["content"])} for f in selected],
                "used_tokens": used, "budget_tokens": budget, "excluded_files": [f["path"] for f in files if f not in selected]}
        index = {"snapshot_sha256": snapshot, "files": [{"path": f["path"], "sha256": f["sha256"], "tokens": tokens(f["content"]), "kind": "code"} for f in files]}
        return index, pack, data, selected
    index = workspace.index(project)
    if index is None:
        raise EngineError("project_unavailable", 404)
    pack = contextpack.build(project, question, budget, idx=index)
    data = context._task_block(question) + contextpack.render(pack)
    return index, pack, data, []


def _block(block_id, text, source, trust="instruction"):
    return {"id": block_id, "text": text, "source": source, "trust": trust, "sha256": sha256_hex(text)}


async def prepare(engine, draft, mode="mock", profile="quick", project=None, cache=True):
    if engine not in builder.ENGINES:
        raise EngineError("unknown_engine", 404)
    mode = resolve_mode(AGENT_PROVIDER_MODE, mode)
    project = project or builder.EXAMPLE_PROJECTS[engine]
    if project not in ("demo-site", "notes-app", "llm-ops"):
        raise EngineError("unknown_project", 422)
    if engine == "code_engine" and project != "demo-site":
        raise EngineError("code_engine_requires_demo_site", 422)
    passport = passports.passport(project, profile)
    built = builder.build(engine, draft)
    if not built["approvable"]:
        raise EngineError("draft_below_quality_threshold", 422)
    if built["too_long"]:
        raise EngineError("prompt_too_long", 422)
    if any("\x00" in m for m in [built["prompt"], built["draft"]["task_goal"]]):
        raise EngineError("invalid_draft", 422)
    aid = _agent(engine)
    role = configs.ROLES[builder.ENGINES[engine]["role"]]
    contract_id = built["contract"]
    contract = {"id": contract_id, **builder.CONTRACTS[contract_id]}
    engine_rules = builder.ENGINES[engine]["system_prompt"] + "\n\nMethodology:\n" + "\n".join(builder.ENGINES[engine]["methodology"]["steps"])
    if contract_id != "unified-diff":
        engine_rules += "\nKeep the exact section headings named in the output contract; the body may use the user language."
    blocks = [_block("engine_prompt", engine_rules, "studio/engines.yaml#" + engine),
              _block("policy", configs.POLICY_DOC["body"], configs.POLICY_DOC["path"]),
              _block("role", role["body"], role["path"])]
    for skill in configs.AGENTS[aid].get("skills", []):
        blocks.append(_block("skill:" + skill, configs.SKILLS[skill]["body"], configs.SKILLS[skill]["path"]))
    blocks.append(_block("contract", contract["output"], "studio/contracts.yaml#" + contract_id))
    prose_words = max(60, passport["limits"]["max_tokens"] // 8)
    budget_instruction = (f"Output budget: at most {prose_words} prose words. Return only the final artifact. "
                          "Complete every required contract section before adding detail; keep each section concise. "
                          "Do not narrate the methodology or repeat the input. If information is missing, state it briefly "
                          "in the required section. Code/diff must also fit the output token limit.")
    blocks.append(_block("output_budget", budget_instruction, "engine_runtime:output-budget-v1"))
    if contract_id == "unified-diff":
        diff_format = ("Final output contract takes precedence over methodology narration. "
                       "Perform planning and review internally; return ONLY a raw unified diff, or exactly NO_CHANGE. "
                       "Do not use Markdown fences, headings, notes, explanations, or diff --git lines. "
                       "The first line must start with --- a/ and the next with +++ b/. "
                       "Use exact source paths from this allowed list, without adding project/directory prefixes: "
                       + ", ".join(ALLOWED_PATHS) + ". "
                       "Calculate exact hunk old/new line counts against supplied source content. "
                       "No text may precede or follow the diff.")
        blocks.append(_block("diff_wire_format", diff_format, "engine_runtime:diff-wire-format-v1"))
    index, pack, data, files = await _source(project, built["draft"]["task_goal"], passport["limits"]["context_budget"], engine == "code_engine")
    blocks.append(_block("draft", "<engine_draft>\n" + built["prompt"] + "\n</engine_draft>", "builder:rendered-user-draft", "data"))
    blocks.append(_block("project_context", data, project + "@" + index["snapshot_sha256"], "data"))
    messages = [{"role": "system", "content": "\n\n".join(b["text"] for b in blocks if b["trust"] == "instruction")},
                {"role": "user", "content": "\n\n".join(b["text"] for b in blocks if b["trust"] == "data")}]
    model = passport["model"]["real_alias"] if mode == "real" else routing.alias(builder.ENGINES[engine]["model_class"])
    gate_hits = outgoing.check(messages, pipeline.gate_rules)
    estimate = pricing.estimate_usd(model, messages, passport["limits"]["max_tokens"])
    packet = {"engine": engine, "role": {"name": builder.ENGINES[engine]["role"], "path": role["path"], "sha256": role["sha256"]},
              "contract": contract, "checker": {"kind": "deterministic", "scope": "output_structure_and_existing_diff_gates_only", "semantic_quality_checked": False},
              "agent": aid, "project": project, "mode": mode, "profile": profile, "cache": cache,
              "messages": messages, "blocks": blocks, "messages_sha256": sha256_hex(canonical_json(messages)),
              "prompt_sha256": built["sha256"], "model": model, "limits": passport["limits"],
              "source_snapshot_sha256": index["snapshot_sha256"], "config_sha256": _signature(engine),
              "output_budget": {"prose_word_hint": prose_words, "max_tokens": passport["limits"]["max_tokens"]},
              "logical_step": "engine:" + engine, "journal_step": "engine-" + engine, "storage_step": "explore",
              "storage_note": "Existing workspace-call SQL schema uses explore; logical Engine identity is preserved in package and journal.",
              "synthetic_fixture": mode == "mock", "outgoing_gate": {"ok": not gate_hits, "hits": gate_hits},
              "estimate_usd": None if estimate is None else float(estimate), "coverage": {"used_tokens": pack["used_tokens"], "context_budget": passport["limits"]["context_budget"]}}
    packet["packet_sha256"] = sha256_hex(canonical_json(packet))
    key = str(uuid4())
    async with LOCK:
        now = time.monotonic()
        for old in [k for k, v in PREPARED.items() if v["expires"] <= now]: PREPARED.pop(old)
        if len(PREPARED) >= MAX_PREPARED: raise EngineError("prepared_capacity_reached", 429)
        PREPARED[key] = {"packet": deepcopy(packet), "built": deepcopy(built), "passport": passport,
                         "index": index, "pack": pack, "files": deepcopy(files), "expires": now + TTL_SECONDS}
    return {"preparation_id": key, "packet": packet, "expires_in_seconds": TTL_SECONDS}


def validate_contract(contract, output, files=None):
    """Structural checks only; no claim of factual truth, architectural quality or language proof."""
    if contract == "unified-diff":
        if output.strip() == "NO_CHANGE": return [{"gate": "diff_or_no_change", "ok": True}]
        checks, _ = gates.run_gates(output, files or [])
        return checks
    headings = [re.sub(r"[^\w ]", "", h).strip().lower() for h in re.findall(r"^#{1,6}\s+(.+?)\s*$", output, re.M)]
    required = {"research-report": ["Goals", "Sources", "Approaches", "Synthesis", "Uncertainties"],
                "adr": ["Context", "Invariants", "Decision", "Data flow", "Risks", "Consequences"],
                "review-report": ["Blockers", "Suggestions"]}
    if contract == "docs-page":
        levels = [len(m) for m in re.findall(r"^(#{1,6})\s+", output, re.M)]
        ok = 1 in levels and 2 in levels and 3 in levels
        has_example = bool(re.search(r"(?i)(usage|example|приклад|використання)", output))
        return [{"gate": "sections_present", "ok": ok, "required": ["h1", "h2", "h3"], "scope": "structure_only"},
                {"gate": "usage_example_present", "ok": has_example, "semantic_quality_checked": False}]
    missing = [h for h in required[contract] if h.lower() not in headings]
    checks = [{"gate": "sections_present", "ok": not missing, "missing": missing, "scope": "structure_only"}]
    if contract == "adr": checks.append({"gate": "mermaid_present", "ok": bool(re.search(r"```mermaid\s*\n.+?\n```", output, re.S)), "syntax_validated": False})
    return checks


def _strip(packet):
    return {k: ([{a: v for a, v in b.items() if a != "text"} for b in value] if k == "blocks" else value)
            for k, value in packet.items() if k != "messages"}


async def run(preparation_id):
    async with LOCK:
        item = PREPARED.get(preparation_id)
        if item is None or item["expires"] <= time.monotonic():
            PREPARED.pop(preparation_id, None); raise EngineError("preparation_missing_or_expired")
        packet = item["packet"]
        current_passport = passports.passport(packet["project"], packet["profile"])
        if current_passport["fingerprint"] != item["passport"]["fingerprint"]:
            PREPARED.pop(preparation_id); raise EngineError("policy_changed_prepare_again")
        if _signature(packet["engine"]) != packet["config_sha256"]:
            PREPARED.pop(preparation_id); raise EngineError("configuration_changed_prepare_again")
        current, _, _, _ = await _source(packet["project"], item["built"]["draft"]["task_goal"], packet["limits"]["context_budget"], packet["engine"] == "code_engine")
        if current["snapshot_sha256"] != packet["source_snapshot_sha256"]:
            PREPARED.pop(preparation_id); raise EngineError("project_changed_prepare_again")
        PREPARED.pop(preparation_id)
    run_id = uuid4()
    repo = pipeline.repo
    meta = {"kind": "engine-run", **_strip(packet)}
    # Do not store user prompts/context bodies in package JSON.
    await repo.save_workspace_snapshot(packet["project"], item["index"]["snapshot_sha256"],
                                       [{k: f[k] for k in ("path", "sha256", "tokens", "kind")} for f in item["index"]["files"]])
    await repo.create_context_run(run_id, packet["project"], item["index"]["snapshot_sha256"],
                                  packet["agent"], packet["mode"], ("[withheld by secret pattern]" if any(p.search(item["built"]["draft"]["task_goal"]) for p in (*pipeline.gate_rules.secrets, *pipeline.gate_rules.denylist)) else item["built"]["draft"]["task_goal"]), packet["limits"]["context_budget"],
                                  {"used_tokens": item["pack"]["used_tokens"], "budget_tokens": item["pack"]["budget_tokens"],
                                   "items": [{k: v for k, v in chunk.items() if k in ("path", "start", "end", "tokens", "sha256", "file_sha256")} for chunk in item["pack"]["items"]]}, meta,
                                  sha256_hex(canonical_json(item["pack"])), packet["messages_sha256"])
    async def insert(run_id, seq, logical_step, *args, **kwargs):
        await repo.insert_workspace_call(run_id, seq, "explore", *args, **kwargs)
    async def complete(messages, model, mode, **kwargs):
        if mode == "mock":
            return await enginefake.complete(packet["engine"], messages, model, max_tokens=kwargs.get("max_tokens"))
        return await pipeline.llm.complete(messages, model, mode, **kwargs)
    runner = pipeline._Run(run_id, packet["mode"], packet["cache"], insert=insert,
                           max_tokens=packet["limits"]["max_tokens"], execution_profile={"engine": packet["engine"], "profile": packet["profile"]},
                           rules_snapshot={"config_sha256": packet["config_sha256"], "passport": item["passport"]["fingerprint"]}, complete=complete)
    prompt = AgentPrompt("engine:" + packet["engine"], "draft-" + packet["prompt_sha256"][:12], None,
                         item["built"]["prompt"], packet["prompt_sha256"])
    checks, output, error = [], "", None
    try:
        result = await runner.call(packet["journal_step"], prompt, packet["messages"], packet["model"])
        if runner.blocked:
            status, checks, error = runner.blocked["status"], runner.blocked["gates"], runner.blocked["error"]
            output = runner.blocked["message"]
        elif not result.ok:
            status, error = "provider_error", result.error
            output = pipeline.TEXT_PROVIDER_ERROR
        else:
            output = pipeline.unfence(result.content)
            leak = chat.gate_reply_leak(output, packet["messages"][0]["content"])
            secret = any(p.search(output) for p in (*pipeline.gate_rules.secrets, *pipeline.gate_rules.denylist))
            checks = [leak, {"gate": "output_secret_pattern", "ok": not secret}]
            if not leak["ok"] or secret:
                status, output = "blocked_by_output_gate", "Engine output was withheld by the output gate."
            else:
                checks += validate_contract(packet["contract"]["id"], output, item["files"])
                status = "completed" if all(c["ok"] for c in checks) else "output_contract_failed"
    except (JournalError, OSError) as exc:
        status, error, output = "execution_uncertain", type(exc).__name__, "Inspect the execution journal; this prepared run cannot be repeated."
    total = runner.total_cost_usd
    await repo.finish_context_run(run_id, status=status, answer=output.replace("\x00", "\\x00"), citations=[], missing=[], gate_report=checks,
                                  prompt_tokens=runner.prompt_tokens, completion_tokens=runner.completion_tokens, total_cost_usd=total, error=error)
    return {"run_id": str(run_id), "engine": packet["engine"], "role": packet["role"]["name"], "contract": packet["contract"]["id"],
            "checker": packet["checker"], "status": status, "output": output, "checks": checks, "model": runner.model,
            "calls": runner.calls, "cache_hits": runner.cache_hits, "cost_usd": None if total is None else float(total),
            "tokens": {"prompt": runner.prompt_tokens, "completion": runner.completion_tokens}, "mode": packet["mode"],
            "profile": packet["profile"], "packet_sha256": packet["packet_sha256"], "messages_sha256": packet["messages_sha256"],
            "synthetic_fixture": packet["synthetic_fixture"], "logical_step": packet["logical_step"], "storage_step": "explore", "error": error}


async def get_run(run_id):
    row = await pipeline.repo.get_context_run(run_id)
    if row is None or row.get("package", {}).get("kind") != "engine-run": return None
    package = row["package"]
    calls = row["calls"]
    return {"run_id": str(row["run_id"]), "engine": package["engine"], "role": package["role"]["name"],
            "contract": package["contract"]["id"], "status": row["status"], "output": row.get("answer"),
            "mode": row["mode"], "profile": package["profile"], "checks": row.get("gate_report", []),
            "calls": sum(not c.get("cache_hit") for c in calls), "cache_hits": sum(bool(c.get("cache_hit")) for c in calls),
            "model": calls[-1]["model"] if calls else None,
            "cost_usd": None if row.get("total_cost_usd") is None else float(row["total_cost_usd"]),
            "package": package, "call_records": calls, "logical_step": package["logical_step"], "storage_step": "explore",
            "checker": package["checker"], "synthetic_fixture": package["synthetic_fixture"],
            "packet_sha256": package["packet_sha256"], "messages_sha256": package["messages_sha256"],
            "tokens": {"prompt": row["total_prompt_tokens"], "completion": row["total_completion_tokens"]}}


async def list_runs(limit=30):
    rows = await pipeline.repo.list_context_runs(100)
    result = []
    for row in rows:
        item = await get_run(row["run_id"])
        if item:
            result.append({k: v for k, v in item.items() if k not in ("output", "package", "call_records")})
            if len(result) >= limit: break
    return {"runs": result, "scanned_recent_context_runs": len(rows), "scan_limit": 100}
