"""Конвеєр dev-агента: 1 задача = N викликів моделі (hw1: plan, потім propose).

Механізм уроку W1 на агенті: кожен крок бере активну версію свого промпта з реєстру agent_prompts,
і в кожен рядок agent_calls пишуться версія та sha256 тексту, який справді пішов у модель.
Механізм уроку W2 (hw2): модель для кроку обирає routing.route_task за складністю задачі, а кожен
виклик отримує cost_usd за синтетичними цінами агента; total_cost_usd задачі = сума її викликів.
R-1: mode="real" — ті самі кроки на DeepSeek через gateway. Перед кожним real-викликом два гейти:
гейт вихідного контексту (outgoing) і бюджет-гард (денна стеля); спрацював — виклику немає, задача
blocked_by_context_gate / blocked_by_budget. Відповідь у ```-огорожі розгортаємо (живі моделі так пишуть).
W3: кеш кроків (cache.py) — та сама відповідь на той самий ключ без виклику моделі; hit пишеться рядком
agent_calls з provider='cache' і cost_usd 0; real-hit не проходить гейти, бо нічого не відправляється.
W4: гейти forbidden_patterns, size_limits і base_version (dry-run diff на файлах контексту). Пропозиція на
base=current, що пройшла всі гейти, стає заявкою (approvals.py): застосовує її лише людина.
Усі доменні результати — HTTP 200 зі статусом задачі; 503 — лише коли недоступна агентська БД.
"""

import json
import asyncio
import weakref
from dataclasses import asdict
import logging
from uuid import uuid4

import re

from sandbox.agent import approvals, configs, context, gates, outgoing, pricing, routing
from sandbox.agent.execution import Journal, JournalError, envelope
from sandbox.agent.cache import Entry, StepCache
from sandbox.agent.patches import looks_like_diff
from sandbox.agent.prompts import AgentPromptRegistry
from sandbox.agent.repo import AgentDatabase, AgentDbUnavailable, AgentRepo
from sandbox.agent.settings import (AGENT_CALL_TIMEOUT_S, AGENT_FAKE_DELAY_MS, AGENT_REAL_BUDGET_USD,
                                    AGENT_REAL_MAX_TOKENS, AGENT_CACHE_MAX, AGENT_CACHE_TTL_S, ALLOWED_PATHS,
                                    EDITOR_PROMPT, PLANNER_PROMPT)
from sandbox.agent.state import AGENT_STATE
from sandbox.core.config import DATABASE_URL, GATEWAY_URL
from sandbox.core.hashing import sha256_hex
from sandbox.llm.fake_provider import FakeProvider
from sandbox.llm.gateway_provider import GatewayProvider
from sandbox.llm.provider_router import LLMRouter
from sandbox.llm.types import LLMResult

log = logging.getLogger("sandbox.agent")

# тексти для людини — українською; ключі й статуси — англійською
TEXT_PROVIDER_ERROR = "Модель тимчасово недоступна: змін не запропоновано."
TEXT_CLARIFY = "Не зрозуміло, що змінити на сайті. Уточніть задачу: шапка, підвал, колір чи аналітика."
TEXT_NO_CHANGE = "Змін не потрібно: сайт уже відповідає задачі."
TEXT_NO_PATCH = "Модель не повернула diff: перевірте активну версію промпта agent-editor."
TEXT_CONTEXT_GATE = "Контекст не відправлено: гейт вихідного контексту знайшов секрет або приватне ім'я."
TEXT_BUDGET = "Реальну модель не викликано: денний бюджет вичерпано."

# лінива БД: об'єкти створюються на імпорті, але до Postgres ніхто не ходить до першого запиту
db = AgentDatabase(DATABASE_URL)
repo = AgentRepo(db)
registry = AgentPromptRegistry(repo)
llm = LLMRouter(FakeProvider(delay_ms=AGENT_FAKE_DELAY_MS), gateway=GatewayProvider("agent", GATEWAY_URL),
                timeout_s=AGENT_CALL_TIMEOUT_S)
gate_rules = outgoing.load()
real_budget_usd = AGENT_REAL_BUDGET_USD
step_cache = StepCache(AGENT_CACHE_TTL_S, AGENT_CACHE_MAX)

# One uvicorn event loop owns SQL accounting and local reservations.
# Provider I/O is outside this lock; multiple server workers are unsupported.
_BUDGET_LOCKS = weakref.WeakKeyDictionary()

def budget_lock():
    loop = asyncio.get_running_loop()
    return _BUDGET_LOCKS.setdefault(loop, asyncio.Lock())

# перевід рядка перед закривною огорожею лишаємо вмісту: diff має закінчуватися на \n
_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*\n(.*?\n?)```\s*$", re.DOTALL)


def unfence(text: str) -> str:
    """```json ... ``` / ```diff ... ``` -> вміст. Без огорожі текст не змінюється."""
    m = _FENCE.match(text)
    return m.group(1) if m else text


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
        data = json.loads(unfence(content))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    intent, files = data.get("intent"), data.get("files")
    if not isinstance(intent, str) or not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        return None
    return {"intent": intent, "files": files, "complexity": data.get("complexity")}


class _Run:
    """Стан однієї задачі: рахує виклики, токени й вартість, пише кожен виклик рядком agent_calls."""

    def __init__(self, task_id, mode: str, use_cache: bool = True, insert=None,
                 max_tokens=None, execution_profile=None, rules_snapshot=None, complete=None):
        self.complete = complete  # optional agent-only adapter; default gateway/fake routing is unchanged
        self.max_tokens = max_tokens if max_tokens is not None else (AGENT_REAL_MAX_TOKENS if mode == "real" else None)
        self.execution_profile = json.loads(json.dumps(execution_profile))
        self.rules_snapshot = json.loads(json.dumps(rules_snapshot))
        self.rules = gate_rules
        self.task_id = task_id
        self.insert = insert                # W5: хід чату пише виклики в agent_chat_calls, а не в agent_calls
        self.mode = mode
        self.use_cache = use_cache
        self.seq = 0                        # номер рядка agent_calls: виклики моделі й cache-hit разом
        self.cache_hits = 0
        self.calls = 0                      # лише справжні виклики моделі
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.model = None
        self.costs: list = []
        self.blocked: dict | None = None    # R-1: гейт або бюджет зупинив real-виклик до відправки

    @property
    def total_cost_usd(self):
        return pricing.total_cost(self.costs)

    async def _guard(self, messages: list[dict], model: str) -> dict | None:
        hits = outgoing.check(messages, self.rules)
        if hits:
            return {"status": "blocked_by_context_gate", "message": TEXT_CONTEXT_GATE, "error": "context_gate",
                    "gates": [{"gate": "outgoing_context", "ok": False, "hits": hits}]}
        estimate = pricing.estimate_usd(model, messages, self.max_tokens or AGENT_REAL_MAX_TOKENS)
        spent = (await repo.cost_today())["real_today_usd"]
        if estimate is None or float(spent) + float(estimate) > real_budget_usd:
            return {"status": "blocked_by_budget", "message": TEXT_BUDGET, "error": "budget",
                    "gates": [{"gate": "real_budget", "ok": False, "spent_usd": float(spent),
                               "estimate_usd": None if estimate is None else float(estimate),
                               "budget_usd": real_budget_usd}]}
        return None

    async def call(self, step: str, prompt, messages: list[dict], model: str):
        messages = json.loads(json.dumps(messages))  # freeze caller-owned nested dictionaries
        key = step_cache.key(self.mode, step, model, prompt, messages)
        if self.execution_profile is not None or self.rules_snapshot is not None or self.max_tokens != (AGENT_REAL_MAX_TOKENS if self.mode == "real" else None):
            from sandbox.agent.execution import digest
            key = digest({"base": key, "max_tokens": self.max_tokens, "profile": self.execution_profile,
                          "rules": self.rules_snapshot})
        hit = step_cache.get(key) if self.use_cache else None
        if hit is not None:
            # відповідь із кешу: моделі не викликаємо, гейти real не потрібні — нічого не відправляється
            self.seq += 1
            self.cache_hits += 1
            self.model = hit.model
            self.costs.append(pricing.ZERO)
            await (self.insert or repo.insert_call)(self.task_id, self.seq, step, "cache", hit.model,
                                   prompt.name, prompt.version, prompt.sha256, 0, 0, 0, "200", pricing.ZERO,
                                   cache_hit=True)
            return LLMResult(ok=True, status=200, content=hit.content, provider="cache", model_alias=hit.model)
        if self.mode == "real" and not Journal().has_step(self.task_id, f"{self.seq + 1}-{step}"):
            self.blocked = await self._guard(messages, model)
            if self.blocked:
                # виклику не було: рядка agent_calls немає, грошей не витрачено
                return LLMResult(ok=False, status=0, content="", provider="gateway", model_alias=model,
                                 error=self.blocked["error"])
        journal = Journal()
        step_id = f"{self.seq + 1}-{step}"
        max_tokens = self.max_tokens
        policy = {"budget": real_budget_usd, "profile": self.execution_profile, "rules_snapshot": self.rules_snapshot, "gate_secrets": [p.pattern for p in self.rules.secrets],
                  "gate_denylist_hash": sha256_hex(str([p.pattern for p in self.rules.denylist]))}
        frozen = envelope(self.mode, step, model, prompt, messages, max_tokens, policy)
        # Only hashes of policy; scan bodies before saving, including mock inputs/results.
        def safe_body(body):
            raw = json.dumps(body, ensure_ascii=False)
            return not any(p.search(raw) for p in (*self.rules.secrets, *self.rules.denylist))
        estimate = pricing.estimate_usd(model, messages, self.max_tokens or AGENT_REAL_MAX_TOKENS) if self.mode == "real" else 0
        try:
            async with budget_lock():
                spent = (await repo.cost_today())["real_today_usd"] if self.mode == "real" else 0
                saved = journal.prepare(self.task_id, step_id, frozen,
                                        messages if safe_body(messages) else None,
                                        estimate=estimate or 0, spent=spent,
                                        budget=real_budget_usd if self.mode == "real" else None)
        except JournalError as exc:
            if str(exc) == "budget reservations exceeded":
                self.blocked = {"status": "blocked_by_budget", "message": TEXT_BUDGET,
                                "error": "budget", "gates": [{"gate": "real_budget_reservation", "ok": False}]}
                return LLMResult(ok=False, status=0, content="", model_alias=model, error="budget")
            raise
        replay = saved["state"] in ("completed", "accounted")
        if replay:
            from sandbox.llm.types import Usage
            raw = dict(saved["result"])
            raw["usage"] = Usage(**raw["usage"])
            raw["tool_calls"] = tuple(raw.get("tool_calls", ()))
            res = LLMResult(**raw)
        else:
            journal.transition(self.task_id, step_id, "pending")
            try:
                res = await (self.complete or llm.complete)(messages, model, self.mode, attempt=1, max_tokens=max_tokens)
                if not res.ok:
                    journal.transition(self.task_id, step_id, "unknown")
                else:
                    body = asdict(res)
                    journal.transition(self.task_id, step_id, "completed", body if safe_body(body) else None)
            except BaseException:
                # Cancellation/process failure may follow an accepted external request.
                # If the disk itself failed, the durable pending marker still blocks retry.
                try:
                    journal.transition(self.task_id, step_id, "unknown")
                except (OSError, JournalError):
                    pass
                raise
        # ціна лише за відповідь моделі: збій (503/429/таймаут) — NULL, а не 0
        cost = (pricing.cost_usd(res.model_alias, res.usage.prompt_tokens, res.usage.completion_tokens)
                if res.ok else None)
        self.calls += 1
        self.seq += 1
        self.prompt_tokens += res.usage.prompt_tokens
        self.completion_tokens += res.usage.completion_tokens
        self.model = res.model_alias
        self.costs.append(cost)
        AGENT_STATE.record_call(res.ok)
        if not replay or saved["state"] != "accounted":
            async with budget_lock():
                await (self.insert or repo.insert_call)(self.task_id, self.seq, step, res.provider, res.model_alias,
                                   prompt.name, prompt.version, prompt.sha256,
                                   res.usage.prompt_tokens, res.usage.completion_tokens, res.latency_ms,
                                   str(res.status), cost)
                if res.ok:
                    journal.transition(self.task_id, step_id, "accounted")
        if res.ok and self.use_cache:
            step_cache.put(Entry(key, self.mode, step, res.model_alias, f"{prompt.name}@{prompt.version}",
                                 res.content, res.usage.prompt_tokens, res.usage.completion_tokens, cost))
        return res


async def run_task(task_text: str, mode: str, base: str, model: str | None = None,
                   use_cache: bool = True, execution_options: dict | None = None) -> tuple[int, dict]:
    """Повертає (HTTP-код, тіло). mode уже перевірений (real без увімкненого оточення — 403 на вході);
    model — ручний вибір моделі для propose, уже звірений з routing.allowed_models(mode)."""
    task_id = uuid4()
    try:
        return 200, await _run(task_id, task_text, mode, base, model, use_cache, execution_options)
    except (JournalError, OSError) as exc:
        log.warning("agent task_id=%s journal unavailable: %s", task_id, type(exc).__name__)
        return 503, {"error": "execution_journal_unavailable", "task_id": str(task_id)}
    except AgentDbUnavailable as exc:
        # задача, якщо встигла створитися, лишається 'received' — слід незавершеного прогону
        log.warning("agent task_id=%s agent db unavailable: %s", task_id, exc)
        return 503, {"error": "agent_db_unavailable"}


async def _run(task_id, task_text: str, mode: str, base: str, model: str | None = None,
               use_cache: bool = True, execution_options: dict | None = None) -> dict:
    await repo.create_task(task_id, task_text, mode, base)
    AGENT_STATE.tasks += 1
    options = execution_options or {}
    run = _Run(task_id, mode, use_cache, **{k: options[k] for k in ("max_tokens", "execution_profile", "rules_snapshot") if k in options})
    out = {"intent": None, "complexity": None, "prompt_version": None, "context_sha256": None, "base_versions": {},
           "diff": None, "patch": None, "gates": [], "error": None, "agents": [], "approval": None}

    async def finish(status: str, message: str) -> dict:
        diff = out["diff"]
        total = run.total_cost_usd
        await repo.finish_task(
            task_id, status=status, message=message, intent=out["intent"], prompt_version=out["prompt_version"],
            context_sha256=out["context_sha256"], base_versions=out["base_versions"],
            patch_diff=None if diff is None else diff.replace("\x00", "\\x00"),
            patch_sha256=None if diff is None else sha256_hex(diff), gate_report=out["gates"],
            prompt_tokens=run.prompt_tokens, completion_tokens=run.completion_tokens, error=out["error"],
            complexity=out["complexity"], total_cost_usd=total)
        return {"task_id": str(task_id), "status": status, "task_text": task_text, "intent": out["intent"],
                "complexity": out["complexity"], "mode": mode, "base": base, "prompt": {"name": EDITOR_PROMPT, "version": out["prompt_version"]},
                "model": run.model, "calls": run.calls, "cache_hits": run.cache_hits, "cache": use_cache,
                "tokens": {"prompt": run.prompt_tokens, "completion": run.completion_tokens},
                # JSON number; null — жодного оціненого виклику (наприклад, plan упав із 503)
                "cost_usd": None if total is None else float(total),
                "gates": out["gates"], "patch": out["patch"], "agents": out["agents"], "approval": out["approval"],
                "model_choice": "manual" if model else "auto", "message": message}

    # крок 1: plan — намір, файли й складність; службовий виклик, тому завжди дешева модель
    planner = options.get("prompt_snapshots", {}).get("plan") or await registry.get_active(PLANNER_PROMPT)
    # C-1: системне повідомлення = версіонований промпт (формат) + роль і скіли агента з studio/
    planner_agent = configs.pipeline_agent("plan")
    out["agents"].append(configs.stamp(planner_agent))
    res = await run.call("plan", planner, context.plan_messages(context.system_text(planner.body,
                         configs.system_extra(planner_agent)), task_text),
                         routing.route_task("plan", mode=mode))
    if run.blocked:
        out["error"], out["gates"] = run.blocked["error"], run.blocked["gates"]
        return await finish(run.blocked["status"], run.blocked["message"])
    if not res.ok:
        out["error"] = res.error
        return await finish("provider_error", TEXT_PROVIDER_ERROR)
    plan = parse_plan(res.content)
    if plan is None:
        return await finish("needs_clarification", TEXT_CLARIFY)
    out["intent"] = plan["intent"]
    out["complexity"] = routing.task_complexity(task_text, plan["complexity"])
    # лише файли зі списку дозволених і без повторів: editor не побачить нічого поза списком
    wanted = [p for p in dict.fromkeys(plan["files"]) if p in ALLOWED_PATHS]
    if plan["intent"] == "unknown" or not wanted:
        return await finish("needs_clarification", TEXT_CLARIFY)

    files = ([f for f in options["site_files"] if f["path"] in wanted]
             if "site_files" in options else await repo.site_files(wanted, base))
    if not files:
        return await finish("needs_clarification", TEXT_CLARIFY)
    out["context_sha256"], out["base_versions"] = context.digest(files, context.history(files))

    # крок 2: propose — diff до цих файлів; модель за складністю: simple -> fast, complex -> balanced (routing.yaml)
    editor = options.get("prompt_snapshots", {}).get("propose") or await registry.get_active(EDITOR_PROMPT)
    out["prompt_version"] = editor.version
    editor_agent = configs.pipeline_agent("propose")
    out["agents"].append(configs.stamp(editor_agent))
    res = await run.call("propose", editor, context.propose_messages(context.system_text(editor.body,
                         configs.system_extra(editor_agent)), task_text, files),
                         model or routing.route_task("propose", out["complexity"], mode))
    if run.blocked:
        out["error"], out["gates"] = run.blocked["error"], run.blocked["gates"]
        return await finish(run.blocked["status"], run.blocked["message"])
    if not res.ok:
        out["error"] = res.error
        return await finish("provider_error", TEXT_PROVIDER_ERROR)
    content = unfence(res.content)
    if content.strip() == "NO_CHANGE":
        return await finish("no_change", TEXT_NO_CHANGE)
    if not looks_like_diff(content):
        # не diff узагалі: зламаний або відкочений промпт editor (аналог «не знаю» в курсу)
        return await finish("no_patch", TEXT_NO_PATCH)

    out["diff"] = content
    out["gates"], patch = gates.run_gates(content, files)
    paths = patch.paths if patch is not None else []
    out["patch"] = {"files": paths, "sha256": sha256_hex(content), "diff": content.replace("\x00", "\\x00")}
    failed = [g["gate"] for g in out["gates"] if not g["ok"]]
    if failed:
        return await finish("rejected_by_gate", f"Зміни відхилено гейтами: {', '.join(failed)}.")
    if base == "current":
        # заявка пишеться до finish: якщо БД упаде тут, задача лишиться 'received', а не «proposed без заявки»;
        # на base=seed заявки немає — це лише превʼю відносно початкового сайту
        a = await approvals.submit(task_id, content, {p: out["base_versions"][p] for p in paths}, paths)
        out["approval"] = {"approval_id": str(a["approval_id"]), "status": a["status"]}
    return await finish("proposed", _proposal_text(paths, patch.added, patch.removed))
