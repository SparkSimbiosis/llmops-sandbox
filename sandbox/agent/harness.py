"""Local harness orchestration: permission decisions precede model calls.

Prepared requests are bounded, process-local, expire, and are consumed once.
Durable model-step recovery is handled separately by execution.Journal.
"""
from copy import deepcopy
import asyncio
import time
from uuid import uuid4

from sandbox.agent import compiler, configs, contextpack, explore, passports, pipeline, pricing, repopatch, workspace
from sandbox.agent.prompts import AgentPrompt
from sandbox.agent.settings import AGENT_PROVIDER_MODE, ALLOWED_PATHS, resolve_mode
from sandbox.core.hashing import canonical_json, sha256_hex

PREPARED = {}
TTL_SECONDS = 1800
MAX_PREPARED = 50
LOCK = asyncio.Lock()

class HarnessError(ValueError):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def projects():
    return [{"id": "demo-site", "title": "Demo site", "kind": "demo", "available": True},
            *[{k: p[k] for k in ("id", "title", "kind", "available")} for p in workspace.projects()]]


def _prompt(comp):
    meta = comp["prompt"]
    text = next(b["text"] for b in comp["blocks"] if b["id"] == "prompt")
    return AgentPrompt(meta["name"], meta["version"], meta["version_id"], text, meta["sha256"])


def _options(passport):
    return {"max_tokens": passport["limits"]["max_tokens"],
            "execution_profile": {"name": passport["profile"], "passport": passport["fingerprint"]},
            "rules_snapshot": deepcopy(passport), "model_override": passport["model"]["real_alias"]}


async def prepare(project, goal, profile="quick", environment="lab", complexity="easy", risk="normal", mode="mock", cache=True,
                  kind="read"):
    """kind: read — питання до проєкту; change — зміна (demo-site: заявка в БД стенда; git-проєкт: .patch)."""
    if not goal.strip() or len(goal) > 2000 or "\x00" in goal:
        raise HarnessError("invalid_goal", 422)
    if risk not in ("normal", "critical"):
        raise HarnessError("invalid_risk", 422)
    if kind not in ("read", "change"):
        raise HarnessError("invalid_kind", 422)
    if project == "demo-site":
        kind = "change"
    mode = resolve_mode(AGENT_PROVIDER_MODE, mode)
    passport = passports.passport(project, profile, environment, complexity)
    if not passport["permissions"]["prepare_prompts"]:
        raise HarnessError("preparation_forbidden", 403)
    options = _options(passport)
    if project == "demo-site":
        if not passport["permissions"]["propose_demo_changes"]:
            raise HarnessError("demo_changes_forbidden_in_environment", 403)
        files = await pipeline.repo.site_files(list(ALLOWED_PATHS), "current")
        comps = [await compiler.compile_call(configs.pipeline_agent(step), goal, mode, site_files_snapshot=files)
                 for step in ("plan", "propose")]
        prompts = {step: _prompt(comp) for step, comp in zip(("plan", "propose"), comps)}
        snapshot = sha256_hex(canonical_json([{k: f[k] for k in ("path", "version", "sha256")} for f in files]))
        options["site_files"] = deepcopy(files)
        packet = {"steps": comps, "note": "Editor file subset is selected by the planner; prompt versions and base are pinned."}
        prepared = None
        stages = [{"role": "planner", "kind": "model"}, {"role": "editor", "kind": "model"},
                  {"role": "checker", "kind": "deterministic"}, {"role": "human", "kind": "approval"}]
    elif kind == "change":
        if not passport["permissions"]["propose_patches"]:
            raise HarnessError("patch_proposals_forbidden", 403)
        prepared = await repopatch.prepare(project, goal, passport["limits"]["context_budget"], mode)
        if prepared is None:
            raise HarnessError("project_unavailable", 404)
        packet = prepared["package"]
        snapshot = prepared["index"]["snapshot_sha256"]
        prompts = None
        stages = [{"role": "context_builder", "kind": "deterministic"}, {"role": "editor", "kind": "model"},
                  {"role": "checker", "kind": "deterministic"}, {"role": "human", "kind": "approval"}]
    else:
        if not passport["permissions"]["read_workspace"]:
            raise HarnessError("workspace_read_forbidden", 403)
        prepared = await explore.prepare(project, goal, passport["limits"]["context_budget"], None, mode)
        if prepared is None:
            raise HarnessError("project_unavailable", 404)
        packet = prepared["package"]
        snapshot = prepared["index"]["snapshot_sha256"]
        prompts = None
        stages = [{"role": "context_builder", "kind": "deterministic"}, {"role": "explorer", "kind": "model"},
                  {"role": "checker", "kind": "deterministic"}]
    def tune(comp):
        model = passport["model"]["real_alias"]
        comp["estimate"]["real_model"] = model
        comp["estimate"]["real_max_tokens"] = passport["limits"]["max_tokens"]
        cost = pricing.estimate_usd(model, comp["messages"], passport["limits"]["max_tokens"])
        comp["estimate"]["real_upper_usd"] = None if cost is None else float(cost)
        if mode == "real":
            comp["route"]["resolved_model"] = model
    if prepared is not None:
        tune(prepared["package"])
        packet = prepared["package"]
    else:
        for comp in packet["steps"]:
            tune(comp)
    packet = deepcopy(packet)
    packet["execution_limits"] = passport["limits"]
    packet["passport_fingerprint"] = passport["fingerprint"]
    key = str(uuid4())
    async with LOCK:
        now = time.monotonic()
        for expired in [k for k, v in PREPARED.items() if v["expires"] <= now]:
            PREPARED.pop(expired)
        if len(PREPARED) >= MAX_PREPARED:
            raise HarnessError("prepared_capacity_reached", 429)
        PREPARED[key] = {"passport": passport, "goal": goal, "mode": mode, "risk": risk, "kind": kind, "snapshot": snapshot,
                         "prepared": deepcopy(prepared), "prompts": prompts, "stages": stages,
                         "options": options, "cache": bool(cache), "expires": now + TTL_SECONDS}
    # How the context pack was selected (terms, funnel, kept and dropped chunks) without chunk texts.
    context = None if prepared is None else contextpack.summary(prepared["pack"])
    return {"preparation_id": key, "kind": kind, "passport": passport, "stages": stages, "packet": packet, "context": context,
            "risk": risk, "cache": bool(cache), "expires_in_seconds": TTL_SECONDS, "critical_confirmation_required": risk == "critical"}


async def run(preparation_id, confirmed=False):
    async with LOCK:
        item = PREPARED.get(preparation_id)
        if item is None or item["expires"] <= time.monotonic():
            PREPARED.pop(preparation_id, None)
            raise HarnessError("preparation_missing_or_expired")
        if item["risk"] == "critical" and not confirmed:
            raise HarnessError("critical_confirmation_required", 403)
        p = item["passport"]
        current = passports.passport(p["project"], p["profile"], p["environment"], p["complexity"])
        if current["fingerprint"] != p["fingerprint"]:
            PREPARED.pop(preparation_id)
            raise HarnessError("policy_changed_prepare_again")
        if p["project"] == "demo-site":
            files = await pipeline.repo.site_files(list(ALLOWED_PATHS), "current")
            snapshot = sha256_hex(canonical_json([{k: f[k] for k in ("path", "version", "sha256")} for f in files]))
        else:
            index = workspace.index(p["project"])
            snapshot = None if index is None else index["snapshot_sha256"]
        if snapshot != item["snapshot"]:
            PREPARED.pop(preparation_id)
            raise HarnessError("project_changed_prepare_again")
        # Consume before invoking any provider. Repeated HTTP submissions cannot resend.
        PREPARED.pop(preparation_id)
    options = item["options"]
    if p["project"] == "demo-site":
        options["prompt_snapshots"] = item["prompts"]
        code, result = await pipeline.run_task(item["goal"], item["mode"], "current",
                                              model=p["model"]["real_alias"] if item["mode"] == "real" else None,
                                              execution_options=options, use_cache=item["cache"])
    elif item["kind"] == "change":
        code, result = await repopatch.propose_call(p["project"], item["goal"], item["mode"], item["prepared"],
                                                    execution_options=options, use_cache=item["cache"])
    else:
        result = await explore.ask(p["project"], item["goal"], item["mode"], p["limits"]["context_budget"],
                                   prepared=item["prepared"], execution_options=options, use_cache=item["cache"])
        code = 200
    if code != 200:
        raise HarnessError(result.get("error", "execution_failed"), code)
    stages = deepcopy(item["stages"])
    attempted = result.get("calls", 0) + result.get("cache_hits", 0)
    blocked = result.get("status") in ("blocked_by_budget", "blocked_by_context_gate")
    model_number = 0
    input_gates = {"outgoing_context", "real_budget", "real_budget_reservation"}
    checked = any(g.get("gate") not in input_gates for g in result.get("gates", []))
    for stage in stages:
        role = stage["role"]
        if role == "context_builder":
            stage["state"] = "completed"
        elif role == "human":
            stage["state"] = "waiting" if result.get("approval") else "not_reached"
        elif role == "checker":
            stage["state"] = "completed" if checked else "not_reached"
        else:
            model_number += 1
            stage["state"] = "completed" if attempted >= model_number else "not_reached"
            if blocked and model_number == attempted + 1:
                stage["state"] = "blocked"
            elif result.get("status") == "provider_error" and attempted == model_number:
                stage["state"] = "failed"
    return {"passport": p, "kind": item["kind"], "stages": stages, "result": result, "status": result["status"],
            "preparation_id": preparation_id, "risk": item["risk"]}
