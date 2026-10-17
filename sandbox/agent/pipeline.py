"""Конвеєр dev-агента: 1 задача = N викликів моделі (hw1: plan, потім propose).

Механізм уроку W1 на агенті: кожен крок бере активну версію свого промпта з реєстру agent_prompts,
і в кожен рядок agent_calls пишуться версія та sha256 тексту, який справді пішов у модель.
Усі доменні результати — HTTP 200 зі статусом задачі; 503 — лише коли недоступна агентська БД.
"""

import json
import logging
from uuid import uuid4

from sandbox.agent import context, gates
from sandbox.agent.patches import looks_like_diff
from sandbox.agent.prompts import AgentPromptRegistry
from sandbox.agent.repo import AgentDatabase, AgentDbUnavailable, AgentRepo
from sandbox.agent.settings import (AGENT_CALL_TIMEOUT_S, AGENT_FAKE_DELAY_MS, AGENT_MODEL, ALLOWED_PATHS,
                                    EDITOR_PROMPT, PLANNER_PROMPT)
from sandbox.agent.state import AGENT_STATE
from sandbox.core.config import DATABASE_URL
from sandbox.core.hashing import sha256_hex
from sandbox.llm.fake_provider import FakeProvider
from sandbox.llm.provider_router import LLMRouter

log = logging.getLogger("sandbox.agent")

# тексти для людини — українською; ключі й статуси — англійською
TEXT_PROVIDER_ERROR = "Модель тимчасово недоступна: змін не запропоновано."
TEXT_CLARIFY = "Не зрозуміло, що змінити на сайті. Уточніть задачу: шапка, підвал, колір чи аналітика."
TEXT_NO_CHANGE = "Змін не потрібно: сайт уже відповідає задачі."
TEXT_NO_PATCH = "Модель не повернула diff: перевірте активну версію промпта agent-editor."

# лінива БД: об'єкти створюються на імпорті, але до Postgres ніхто не ходить до першого запиту
db = AgentDatabase(DATABASE_URL)
repo = AgentRepo(db)
registry = AgentPromptRegistry(repo)
llm = LLMRouter(FakeProvider(delay_ms=AGENT_FAKE_DELAY_MS), timeout_s=AGENT_CALL_TIMEOUT_S)


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _proposal_text(paths: list[str], added: int, removed: int) -> str:
    parts = [f"{added} {_plural(added, 'доданий', 'додані', 'доданих')} {_plural(added, 'рядок', 'рядки', 'рядків')}"]
    if removed:
        parts.append(f"{removed} {_plural(removed, 'видалений', 'видалені', 'видалених')} "
                     f"{_plural(removed, 'рядок', 'рядки', 'рядків')}")
    return f"Пропоную зміни: {', '.join(paths)} ({', '.join(parts)})."


def parse_plan(content: str) -> dict | None:
    """План від planner — JSON {"intent", "files", "complexity"}. Будь-що інше — план не розпізнано."""
    try:
        data = json.loads(content)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    intent, files = data.get("intent"), data.get("files")
    if not isinstance(intent, str) or not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        return None
    return {"intent": intent, "files": files, "complexity": data.get("complexity")}


class _Run:
    """Стан однієї задачі: рахує виклики й токени, пише кожен виклик рядком agent_calls."""

    def __init__(self, task_id, mode: str):
        self.task_id = task_id
        self.mode = mode
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.model = None

    async def call(self, step: str, prompt, messages: list[dict]):
        res = await llm.complete(messages, AGENT_MODEL, self.mode, attempt=1)
        self.calls += 1
        self.prompt_tokens += res.usage.prompt_tokens
        self.completion_tokens += res.usage.completion_tokens
        self.model = res.model_alias
        AGENT_STATE.record_call(res.ok)
        await repo.insert_call(self.task_id, self.calls, step, res.provider, res.model_alias,
                               prompt.name, prompt.version, prompt.sha256,
                               res.usage.prompt_tokens, res.usage.completion_tokens, res.latency_ms,
                               str(res.status))
        return res


async def run_task(task_text: str, mode: str, base: str) -> tuple[int, dict]:
    """Повертає (HTTP-код, тіло). mode уже перевірений (real відсічено на вході 403)."""
    task_id = uuid4()
    try:
        return 200, await _run(task_id, task_text, mode, base)
    except AgentDbUnavailable as exc:
        # задача, якщо встигла створитися, лишається 'received' — слід незавершеного прогону
        log.warning("agent task_id=%s agent db unavailable: %s", task_id, exc)
        return 503, {"error": "agent_db_unavailable"}


async def _run(task_id, task_text: str, mode: str, base: str) -> dict:
    await repo.create_task(task_id, task_text, mode, base)
    AGENT_STATE.tasks += 1
    run = _Run(task_id, mode)
    out = {"intent": None, "prompt_version": None, "context_sha256": None, "base_versions": {},
           "diff": None, "patch": None, "gates": [], "error": None}

    async def finish(status: str, message: str) -> dict:
        diff = out["diff"]
        await repo.finish_task(
            task_id, status=status, message=message, intent=out["intent"], prompt_version=out["prompt_version"],
            context_sha256=out["context_sha256"], base_versions=out["base_versions"],
            patch_diff=None if diff is None else diff.replace("\x00", "\\x00"),
            patch_sha256=None if diff is None else sha256_hex(diff), gate_report=out["gates"],
            prompt_tokens=run.prompt_tokens, completion_tokens=run.completion_tokens, error=out["error"])
        return {"task_id": str(task_id), "status": status, "task_text": task_text, "intent": out["intent"],
                "mode": mode, "base": base, "prompt": {"name": EDITOR_PROMPT, "version": out["prompt_version"]},
                "model": run.model, "calls": run.calls,
                "tokens": {"prompt": run.prompt_tokens, "completion": run.completion_tokens},
                "cost_usd": None,   # колонок вартості в hw1 немає — їх додає міграція hw2
                "gates": out["gates"], "patch": out["patch"], "message": message}

    # крок 1: plan — намір і файли
    planner = await registry.get_active(PLANNER_PROMPT)
    res = await run.call("plan", planner, context.plan_messages(planner.body, task_text))
    if not res.ok:
        out["error"] = res.error
        return await finish("provider_error", TEXT_PROVIDER_ERROR)
    plan = parse_plan(res.content)
    if plan is None:
        return await finish("needs_clarification", TEXT_CLARIFY)
    out["intent"] = plan["intent"]
    # лише файли зі списку дозволених і без повторів: editor не побачить нічого поза списком
    wanted = [p for p in dict.fromkeys(plan["files"]) if p in ALLOWED_PATHS]
    if plan["intent"] == "unknown" or not wanted:
        return await finish("needs_clarification", TEXT_CLARIFY)

    files = await repo.site_files(wanted, base)
    if not files:
        return await finish("needs_clarification", TEXT_CLARIFY)
    out["context_sha256"], out["base_versions"] = context.digest(files, context.history(files))

    # крок 2: propose — diff до цих файлів
    editor = await registry.get_active(EDITOR_PROMPT)
    out["prompt_version"] = editor.version
    res = await run.call("propose", editor, context.propose_messages(editor.body, task_text, files))
    if not res.ok:
        out["error"] = res.error
        return await finish("provider_error", TEXT_PROVIDER_ERROR)
    content = res.content
    if content.strip() == "NO_CHANGE":
        return await finish("no_change", TEXT_NO_CHANGE)
    if not looks_like_diff(content):
        # не diff узагалі: зламаний або відкочений промпт editor (аналог «не знаю» в курсу)
        return await finish("no_patch", TEXT_NO_PATCH)

    out["diff"] = content
    out["gates"], patch = gates.run_gates(content)
    paths = patch.paths if patch is not None else []
    out["patch"] = {"files": paths, "sha256": sha256_hex(content), "diff": content.replace("\x00", "\\x00")}
    failed = [g["gate"] for g in out["gates"] if not g["ok"]]
    if failed:
        return await finish("rejected_by_gate", f"Зміни відхилено гейтами: {', '.join(failed)}.")
    return await finish("proposed", _proposal_text(paths, patch.added, patch.removed))
