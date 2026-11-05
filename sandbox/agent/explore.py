"""Дослідник проєкту (W6): питання до реального репозиторію -> пакет контексту -> prompt-package -> відповідь із
посиланнями, перевірена кодом.

Прогін = один виклик моделі (крок explore). Що піде в модель, вирішують код і конфігурація, а не модель:
- конструктор контексту (contextpack.build) відбирає фрагменти файлів під питання в межах бюджету токенів;
- prompt-package — це рецепт explore з context.yaml, зібраний компілятором: версіонований промпт agent-explorer
  (формат explore json), політика, роль explorer, скіл cite-sources, картка контракту answer-with-citations, далі
  дані: питання, перелік файлів проєкту і пакет. Кожен блок — з джерелом, довірою (instruction / data) і sha256;
  package_sha256 = sha256 повідомлень, що справді пішли в модель;
- після моделі — гейти: формат (explore_format), посилання лише на рядки з пакета (citations), відповідь не
  повторює службових інструкцій (reply_leak, як у чаті).
Real — ті самі гейт вихідного контексту й бюджет, що в задач; виклики — у agent_workspace_calls і в /agent/cost.
"""

import json
import logging
import re
from uuid import uuid4

from sandbox.agent import chat, compiler, configs, contextpack, pipeline, workspace
from sandbox.agent.repo import AgentDbUnavailable
from sandbox.agent.prompts import AgentPrompt

log = logging.getLogger("sandbox.agent.explore")

AGENT = "explorer"
TEXT_BAD_FORMAT = "Модель відповіла не у форматі explore json — показую текст як є, посилання не перевірено."
_LINES = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$")
_REF = re.compile(r"^(.+?):L?(\d+)(?:-L?(\d+))?$")


def _citation(raw) -> dict | None:
    """{"path", "lines": "12-18"} | {"path", "start", "end"} | "path:12-18" -> {path, start, end}; інше — None."""
    if isinstance(raw, str):
        m = _REF.match(raw.strip())
        if not m:
            return None
        a = int(m.group(2))
        return {"path": m.group(1), "start": a, "end": int(m.group(3) or a)}
    if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
        return None
    if isinstance(raw.get("lines"), (str, int)):
        m = _LINES.match(str(raw["lines"]))
        if not m:
            return None
        a = int(m.group(1))
        return {"path": raw["path"], "start": a, "end": int(m.group(2) or a)}
    if isinstance(raw.get("start"), int):
        return {"path": raw["path"], "start": raw["start"], "end": raw.get("end") or raw["start"]}
    return None


def parse_answer(content: str) -> dict | None:
    """explore json -> {answer, citations: [raw], missing: [str]}; None — це не explore json."""
    raw = pipeline.unfence(content).strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if m is None:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("answer"), str):
        return None
    cites = data.get("citations") if isinstance(data.get("citations"), list) else []
    missing = [x for x in data.get("missing") or [] if isinstance(x, str)] if isinstance(data.get("missing"), list) else []
    return {"answer": data["answer"], "citations": cites[:20], "missing": missing[:10]}


def gate_citations(citations: list, pack: dict) -> dict:
    """Кожне посилання — шлях і рядки всередині одного фрагмента пакета. Інакше модель послалася на те, чого не
    бачила (вигадала файл або рядки) — гейт це показує, а не вірить на слово."""
    out = []
    for raw in citations:
        c = _citation(raw)
        if c is None or c["end"] < c["start"]:
            out.append({"raw": raw, "ok": False, "reason": "bad_ref"})
            continue
        item = next((i for i in pack["items"] if i["path"] == c["path"]
                     and i["start"] <= c["start"] and c["end"] <= i["end"]), None)
        reason = None if item else ("not_in_pack" if not any(i["path"] == c["path"] for i in pack["items"])
                                    else "lines_outside_chunk")
        out.append({**c, "ok": item is not None, "chunk": item["id"] if item else None, "reason": reason})
    bad = [c for c in out if not c["ok"]]
    return {"gate": "citations", "ok": bool(out) and not bad, "total": len(out), "invalid": len(bad),
            "items": out, "validation_scope": "provided_ranges_only", "semantic_support_checked": False}


def package(comp: dict, pack: dict, agent_id: str = AGENT) -> dict:
    """Prompt-package: що саме й з чого зібрано. Блоки — з текстом (для UI); у БД — без текстів.
    agent_id — агент рецепта (explorer; repo_editor для .patch git-проєкту)."""
    agent = configs.AGENTS[agent_id]
    return {"agent": agent_id, "title": agent["title"], "role": agent["role"], "skills": agent.get("skills", []),
            "contract": {"id": agent["contract"], **{k: v for k, v in compiler.builder.CONTRACTS[agent["contract"]].items()
                                                     if k in ("title", "output", "checks")}},
            "recipe": comp["recipe"], "route": comp["route"], "estimate": comp["estimate"],
            "prompt": comp["prompt"],
            "outgoing_gate": comp["outgoing_gate"], "blocks": comp["blocks"], "messages": comp["messages"],
            "package_sha256": comp["messages_sha256"], "pack_sha256": pack["pack_sha256"],
            "stamp": configs.stamp(agent_id)}


def _strip(package: dict) -> dict:
    return {k: ([{x: v for x, v in b.items() if x != "text"} for b in package["blocks"]] if k == "blocks" else v)
            for k, v in package.items() if k != "messages"}


async def prepare(project: str, question: str, budget: int, pinned: list[str] | None, mode: str) -> dict | None:
    """Пакет контексту + prompt-package без виклику моделі (кнопка «Зібрати пакет»). None — проєкт недоступний."""
    idx = workspace.index(project)
    if idx is None:
        return None
    pack = contextpack.build(project, question, budget, pinned, idx)
    comp = await compiler.compile_call(AGENT, question, mode, ws={"index": idx, "pack": pack})
    return {"index": idx, "pack": pack, "package": package(comp, pack)}


async def ask(project: str, question: str, mode: str, budget: int = contextpack.BUDGET_DEFAULT,
              pinned: list[str] | None = None, use_cache: bool = True, *,
              prepared: dict | None = None, execution_options: dict | None = None) -> dict | None:
    prep = prepared if prepared is not None else await prepare(project, question, budget, pinned, mode)
    if prep is None:
        return None
    idx, pack, package = prep["index"], prep["pack"], prep["package"]
    repo = pipeline.repo
    if execution_options:
        package["execution_profile"] = execution_options.get("execution_profile")
        package["effective_rules"] = execution_options.get("rules_snapshot")
    run_id = uuid4()
    await repo.save_workspace_snapshot(project, idx["snapshot_sha256"],
                                       [{k: f[k] for k in ("path", "sha256", "tokens", "kind")} for f in idx["files"]])
    await repo.create_context_run(run_id, project, idx["snapshot_sha256"], AGENT, mode, question,
                                  pack["budget_tokens"], contextpack.summary(pack), _strip(package),
                                  pack["pack_sha256"], package["package_sha256"])
    options = execution_options or {}
    run = pipeline._Run(run_id, mode, use_cache, insert=repo.insert_workspace_call,
                        **{k: options[k] for k in ("max_tokens", "execution_profile", "rules_snapshot") if k in options})
    out = {"gates": [], "error": None, "answer": None, "citations": [], "missing": []}

    async def finish(status: str, answer: str) -> dict:
        total = run.total_cost_usd
        await repo.finish_context_run(run_id, status=status, answer=answer, citations=out["citations"],
                                      missing=out["missing"], gate_report=out["gates"],
                                      prompt_tokens=run.prompt_tokens, completion_tokens=run.completion_tokens,
                                      total_cost_usd=total, error=out["error"])
        return {"run_id": str(run_id), "project": project, "mode": mode, "status": status, "question": question,
                "answer": answer, "citations": out["citations"], "missing": out["missing"], "gates": out["gates"],
                "model": run.model, "calls": run.calls, "cache_hits": run.cache_hits, "error": out["error"],
                "tokens": {"prompt": run.prompt_tokens, "completion": run.completion_tokens},
                "cost_usd": None if total is None else float(total), "pack": pack, "package": package}

    # Reconstruct the immutable prompt resolved during compilation; never read active version again.
    meta = package["prompt"]
    body = next(b["text"] for b in package["blocks"] if b["id"] == "prompt")
    prompt = AgentPrompt(meta["name"], meta["version"], meta["version_id"], body, meta["sha256"])
    messages = package["messages"]
    res = await run.call("explore", prompt, messages, (options.get("model_override") if mode == "real" else None) or pipeline.routing.route_task("explore", mode=mode))
    if run.blocked:
        out["error"], out["gates"] = run.blocked["error"], run.blocked["gates"]
        return await finish(run.blocked["status"], run.blocked["message"])
    if not res.ok:
        out["error"] = res.error
        return await finish("provider_error", pipeline.TEXT_PROVIDER_ERROR)

    parsed = parse_answer(res.content)
    out["gates"].append({"gate": "explore_format", "ok": parsed is not None})
    if parsed is None:
        return await finish("bad_format", res.content.strip()[:4000] or TEXT_BAD_FORMAT)
    leak = chat.gate_reply_leak(parsed["answer"], messages[0]["content"])
    out["gates"].append(leak)
    if not leak["ok"]:
        return await finish("blocked_by_output_gate", chat.TEXT_LEAK)
    cites = gate_citations(parsed["citations"], pack)
    out["gates"].append(cites)
    out["citations"], out["missing"] = cites["items"], parsed["missing"]
    if cites["ok"]:
        status = "answered"
    elif not cites["total"]:
        status = "not_in_context"           # модель чесно не знайшла відповіді в пакеті — або не послалася
    else:
        status = "unsupported"
    return await finish(status, parsed["answer"])


async def ask_call(project: str, question: str, mode: str, budget: int, pinned: list[str] | None,
                   use_cache: bool = True) -> tuple[int, dict]:
    try:
        result = await ask(project, question, mode, budget, pinned, use_cache)
    except AgentDbUnavailable as exc:
        log.warning("agent explore project=%s agent db unavailable: %s", project, exc)
        return 503, {"error": "agent_db_unavailable"}
    return (404, {"error": "project not available"}) if result is None else (200, result)
