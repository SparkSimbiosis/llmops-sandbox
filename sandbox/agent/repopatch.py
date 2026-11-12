"""Зміна з підтвердженням для git-проєкту: задача -> пакет контексту -> diff -> гейти -> заявка -> .patch.

Стенд у репозиторій не пише: робоча копія змонтована read-only, а Approve лише ще раз звіряє файли й відкриває
завантаження .patch, який людина застосовує сама (git apply). Що бачить модель, вирішує той самий конструктор
контексту, що й для питань (contextpack): змінювати можна лише файли, фрагменти яких потрапили в пакет.
Промпт — активна версія agent-editor з реєстру (крок propose, як на demo-site), тож відкат промпта ламає й цей шлях;
роль і скіл — під фрагменти з номерами рядків. Відповідь моделі проходить гейти з перерахунком лічильників @@ (як
git apply --recount), а в заявку йде канонічний diff, зібраний з текстів «до» і «після», — саме його й завантажують.
Задача пишеться в agent_tasks (intent repo_patch:<проєкт>), виклик — у agent_calls кроком propose.
"""

import logging
from uuid import uuid4

from sandbox.agent import approvals, compiler, contextpack, explore, gates, pipeline, workspace
from sandbox.agent.execution import JournalError
from sandbox.agent.patches import looks_like_diff, parse_unified_diff, unified_diff
from sandbox.agent.prompts import AgentPrompt
from sandbox.agent.repo import AgentDbUnavailable
from sandbox.agent.state import AGENT_STATE
from sandbox.core.hashing import sha256_hex

log = logging.getLogger("sandbox.agent.repopatch")

AGENT = "repo_editor"
TEXT_NO_CHANGE = "Змін не запропоновано: модель вважає, що файли пакета вже відповідають задачі."
TEXT_AFTER = " Після Approve — .patch для git apply; репозиторій стенд не змінює."


async def prepare(project: str, goal: str, budget: int, mode: str) -> dict | None:
    """Пакет контексту + prompt-package editor без виклику моделі. None — проєкт недоступний."""
    idx = workspace.index(project)
    if idx is None:
        return None
    pack = contextpack.build(project, goal, budget, None, idx)
    comp = await compiler.compile_call(AGENT, goal, mode, ws={"index": idx, "pack": pack})
    return {"index": idx, "pack": pack, "package": explore.package(comp, pack, AGENT)}


def _files(project: str, pack: dict, idx: dict) -> list[dict]:
    """Повні поточні тексти файлів пакета: на них іде dry-run, їхні sha256 — база заявки."""
    out = []
    for path in contextpack.paths(pack):
        text = workspace.read_file(project, path, idx)
        if text is not None:
            out.append({"path": path, "content": text, "version": sha256_hex(text)})
    return out


async def propose(project: str, goal: str, mode: str, prepared: dict, execution_options: dict | None = None,
              use_cache: bool = True) -> dict:
    idx, pack, package = prepared["index"], prepared["pack"], prepared["package"]
    repo = pipeline.repo
    task_id = uuid4()
    await repo.create_task(task_id, goal, mode, "current")
    AGENT_STATE.tasks += 1
    options = execution_options or {}
    run = pipeline._Run(task_id, mode, use_cache,
                        **{k: options[k] for k in ("max_tokens", "execution_profile", "rules_snapshot") if k in options})
    files = _files(project, pack, idx)
    meta = package["prompt"]
    body = next(b["text"] for b in package["blocks"] if b["id"] == "prompt")
    prompt = AgentPrompt(meta["name"], meta["version"], meta["version_id"], body, meta["sha256"])
    out = {"diff": None, "patch": None, "gates": [], "error": None, "approval": None, "base": {}}

    async def finish(status: str, message: str) -> dict:
        diff, total = out["diff"], run.total_cost_usd
        await repo.finish_task(
            task_id, status=status, message=message, intent=f"repo_patch:{project}", prompt_version=prompt.version,
            context_sha256=pack["pack_sha256"], base_versions=out["base"],
            patch_diff=None if diff is None else diff.replace("\x00", "\\x00"),
            patch_sha256=None if diff is None else sha256_hex(diff), gate_report=out["gates"],
            prompt_tokens=run.prompt_tokens, completion_tokens=run.completion_tokens, error=out["error"],
            complexity=None, total_cost_usd=total)
        return {"task_id": str(task_id), "project": project, "status": status, "task_text": goal, "mode": mode,
                "prompt": {"name": prompt.name, "version": prompt.version}, "model": run.model, "calls": run.calls,
                "cache_hits": run.cache_hits, "cache": use_cache,
                "tokens": {"prompt": run.prompt_tokens, "completion": run.completion_tokens},
                "cost_usd": None if total is None else float(total), "gates": out["gates"], "patch": out["patch"],
                "approval": out["approval"], "error": out["error"], "message": message}

    model = (options.get("model_override") if mode == "real" else None) or package["route"]["resolved_model"]
    res = await run.call("propose", prompt, package["messages"], model)
    if run.blocked:
        out["error"], out["gates"] = run.blocked["error"], run.blocked["gates"]
        return await finish(run.blocked["status"], run.blocked["message"])
    if not res.ok:
        out["error"] = res.error
        return await finish("provider_error", pipeline.TEXT_PROVIDER_ERROR)
    content = pipeline.unfence(res.content)
    if content.strip() == "NO_CHANGE":
        return await finish("no_change", TEXT_NO_CHANGE)
    if not looks_like_diff(content):
        return await finish("no_patch", pipeline.TEXT_NO_PATCH)

    out["diff"] = content
    out["gates"], patch, new = gates.run_repo_gates(content, files, pipeline.gate_rules)
    failed = [g["gate"] for g in out["gates"] if not g["ok"]]
    if failed:
        out["patch"] = {"files": patch.paths if patch else [], "sha256": sha256_hex(content), "diff": content.replace("\x00", "\\x00")}
        return await finish("rejected_by_gate", f"Зміни відхилено гейтами: {', '.join(failed)}.")
    base = {f["path"]: f for f in files}
    paths = [p for p in dict.fromkeys(patch.paths) if new[p] != base[p]["content"]]
    canonical = "".join(unified_diff(p, base[p]["content"], new[p]) for p in paths)
    if not canonical:
        return await finish("no_change", TEXT_NO_CHANGE)
    out["base"] = {p: base[p]["version"] for p in paths}
    parsed = parse_unified_diff(canonical)
    out["patch"] = {"files": paths, "sha256": sha256_hex(canonical), "diff": canonical,
                    "model_diff_sha256": sha256_hex(content), "recounted": patch.recounted}
    a = await approvals.submit(task_id, canonical, out["base"], paths, approvals.export_action(project))
    out["approval"] = {"approval_id": str(a["approval_id"]), "status": a["status"], "action": a["action"]}
    return await finish("proposed", pipeline._proposal_text(paths, parsed.added, parsed.removed) + TEXT_AFTER)


async def propose_call(*args, **kwargs) -> tuple[int, dict]:
    """(HTTP-код, тіло), як pipeline.run_task: збій журналу чи БД — 503 без повтору."""
    try:
        return 200, await propose(*args, **kwargs)
    except (JournalError, OSError) as exc:
        log.warning("repo patch journal unavailable: %s", type(exc).__name__)
        return 503, {"error": "execution_journal_unavailable"}
    except AgentDbUnavailable as exc:
        log.warning("repo patch agent db unavailable: %s", exc)
        return 503, {"error": "agent_db_unavailable"}
