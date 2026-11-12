"""Компілятор контексту (C-1): що саме піде в модель для агента — без виклику моделі.

Рецепт із context.yaml -> блоки (промпт, роль, скіли, задача, файли...) -> повідомлення system/user.
Для агентів конвеєра блоки складаються тими самими функціями, що й у pipeline, тож превʼю збігається з
реальним викликом байт у байт. W6: для explorer це і є prompt-package — explore.py відправляє саме ці
повідомлення (роль + скіли + контракт + промпт + пакет контексту), з sha256 кожного блока. Поруч — оцінка токенів, ціна (mock і real), клас моделі за роутингом і
результат гейта вихідного контексту: видно, чи пройшов би цей виклик у real, не витративши ні цента.
"""

from sandbox.agent import builder, configs, context, contextpack, outgoing, pipeline, pricing, routing, workspace
from sandbox.agent.settings import AGENT_REAL_MAX_TOKENS, ALLOWED_PATHS
from sandbox.core.hashing import canonical_json, sha256_hex
from sandbox.llm.fake_provider import tokens

PATCH_PLACEHOLDER = "(diff задачі зʼявиться після кроку propose)"


def _model(agent: dict, task_text: str, mode: str) -> dict:
    alias_mode = "real" if mode == "real" else "test"
    spec = agent.get("model") or {}
    if "step" in spec:
        cls = routing.TABLE["steps"][spec["step"]]
        return {"chosen_model_class": cls, "resolved_model": routing.alias(cls, alias_mode),
                "rule": "step", "reason": "службовий крок — клас із routing.yaml steps"}
    if spec.get("rules"):
        # складності ще немає (її дає planner): рішення за довжиною задачі; у конвеєрі додасться complex
        return routing.decide(None, len(task_text), (), alias_mode)
    cls = spec["class"]
    return {"chosen_model_class": cls, "resolved_model": routing.alias(cls, alias_mode),
            "rule": "agent", "reason": "клас закріплено за агентом в agents.yaml"}


def contract_text(contract_id: str) -> str:
    c = builder.CONTRACTS[contract_id]
    return f"Contract {contract_id}: {c['purpose']}\nOutput: {c['output']}\nChecked by code: {', '.join(c['checks'])}"


async def _block_text(block: str, agent: dict, task_text: str, files: list[dict], patch: str | None,
                      ws: dict | None = None, prompt=None):
    """(текст, версія-підпис) блоку. Звертається до БД лише для prompt і site_files."""
    if block == "prompt":
        p = prompt if prompt is not None else await pipeline.registry.get_active(agent["prompt"])
        return p.body, f"{agent['prompt']}@{p.version}"
    if block == "engine_prompt":
        built = builder.build(agent["engine"], {"title": task_text[:200], "task_goal": task_text,
                                                "input_data": patch or ""})
        return built["prompt"], f"{agent['engine']}#{built['sha256'][:12]}"
    if block == "policy":
        d = configs.POLICY_DOC
        return d["body"], f"{d['path']}#{d['sha256'][:12]}"
    if block == "role":
        r = configs.ROLES[agent["role"]]
        return r["body"], f"{r['path']}#{r['sha256'][:12]}"
    if block == "skills":
        names = agent.get("skills", [])
        text = "\n\n".join(f"Скіл {s}:\n{configs.SKILLS[s]['body']}" for s in names)
        return text, ", ".join(names) or None
    if block == "task":
        return context._task_block(task_text), None
    if block == "allowed_paths":
        return context._allowed_block(), None
    if block == "site_files":
        return "".join(f'<site_file path="{f["path"]}" version="{f["version"]}" sha256="{f["sha256"]}">\n'
                       f'{f["content"]}</site_file>\n' for f in files), ", ".join(f["path"] for f in files)
    if block == "history":
        return "", None                     # у компіляторі історії немає: це перший хід чату
    if block == "patch":
        return f"<patch>\n{patch or PATCH_PLACEHOLDER}\n</patch>\n", None
    if block == "contract":
        text = contract_text(agent["contract"])
        return text, f"{agent['contract']}#{sha256_hex(text)[:12]}"
    if block == "project_files":
        return contextpack.tree(ws["index"]), f"{ws['index']['project']}@{ws['index']['snapshot_sha256'][:12]}"
    if block == "context_pack":
        p = ws["pack"]
        return contextpack.render(p), f"{len(p['items'])} фрагм., pack#{p['pack_sha256'][:12]}"
    if block == "pack_paths":
        paths = contextpack.paths(ws["pack"])
        return "<allowed_paths>\n" + "".join(f"{p}\n" for p in paths) + "</allowed_paths>\n", f"{len(paths)} файл."
    raise KeyError(block)


def default_workspace(task_text: str) -> dict | None:
    """Компілятор без проєкту (вкладка «Конфігурація»): пакет під задачу в тестовому репозиторії."""
    idx = workspace.index(workspace.DEFAULT_PROJECT)
    return None if idx is None else {"index": idx, "pack": contextpack.build(idx["project"], task_text, idx=idx)}


async def compile_call(agent_id: str, task_text: str, mode: str = "mock", paths: list[str] | None = None,
                       base: str = "current", patch: str | None = None, ws: dict | None = None,
                       prompt=None, site_files_snapshot: list[dict] | None = None) -> dict | None:
    """None — немає такого агента. AgentDbUnavailable летить нагору (маршрут віддає 503).
    ws — {index, pack} робочого простору для рецепта explore (W6)."""
    agent = configs.AGENTS.get(agent_id)
    if agent is None:
        return None
    recipe = configs.RECIPES[agent["context"]]
    if "prompt" in recipe["system"] + recipe["user"] and prompt is None:
        prompt = await pipeline.registry.get_active(agent["prompt"])
    if ws is None and {"project_files", "context_pack", "pack_paths"} & set(recipe["user"]):
        ws = default_workspace(task_text)
    files = []
    if "site_files" in recipe["user"]:
        wanted = [p for p in dict.fromkeys(paths or ALLOWED_PATHS) if p in ALLOWED_PATHS]
        files = ([f for f in site_files_snapshot if f["path"] in wanted] if site_files_snapshot is not None
                 else await pipeline.repo.site_files(wanted, base))

    blocks, texts = [], {"system": [], "user": []}
    for where in ("system", "user"):
        for b in recipe[where]:
            text, ref = await _block_text(b, agent, task_text, files, patch, ws, prompt)
            if not text:
                continue
            meta = configs.BLOCKS[b]
            blocks.append({"id": b, "title": meta["title"], "where": where, "trust": meta["trust"],
                           "source": meta["source"], "ref": ref, "chars": len(text), "tokens": tokens(text),
                           "sha256": sha256_hex(text), "text": text})
            texts[where].append(text)
    # системні блоки — через порожній рядок (як context.system_text), користувацькі — підряд (як у context)
    messages = [{"role": "system", "content": "\n\n".join(texts["system"])},
                {"role": "user", "content": "".join(texts["user"])}]

    route = _model(agent, task_text, mode)
    test_model = route["resolved_model"] if mode != "real" else _model(agent, task_text, "mock")["resolved_model"]
    real_model = _model(agent, task_text, "real")["resolved_model"]
    prompt_tokens = sum(tokens(m["content"]) for m in messages)
    test_cost = pricing.cost_usd(test_model, prompt_tokens, 0)
    real_cost = pricing.estimate_usd(real_model, messages, AGENT_REAL_MAX_TOKENS)
    hits = outgoing.check(messages, pipeline.gate_rules)
    return {"agent": agent_id, "step": agent["step"], "pipeline": agent["pipeline"], "recipe": agent["context"],
            "contract": agent["contract"], "route": route, "blocks": blocks, "messages": messages,
            "prompt": None if prompt is None else {"name": prompt.name, "version": prompt.version,
                         "version_id": str(prompt.version_id) if prompt.version_id is not None else None,
                         "sha256": prompt.sha256},
            "messages_sha256": sha256_hex(canonical_json(messages)),
            "estimate": {"prompt_tokens": prompt_tokens,
                         "test_model": test_model, "test_input_usd": None if test_cost is None else float(test_cost),
                         "real_model": real_model, "real_max_tokens": AGENT_REAL_MAX_TOKENS,
                         "real_upper_usd": None if real_cost is None else float(real_cost)},
            "outgoing_gate": {"ok": not hits, "hits": hits}}
