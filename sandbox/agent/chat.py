"""Чат з агентом (W5): агент читає сайт і відповідає, а будь-яку зміну лише пропонує.

Хід чату = один виклик моделі (крок chat) з файлами сайту, короткою історією й повідомленням людини.
Відповідь — chat json: {"reply": ..., "proposal": null | "задача для виконавця"}. Пропозиція не пише
нічого сама: вона йде звичайним конвеєром (plan -> propose -> гейти) і, якщо пройде, стає заявкою,
яку застосовує лише людина. У чаті немає інструмента apply — це забезпечено кодом, а не промптом.

Жорсткі шари поверх моделі: гейт вихідного контексту й бюджет (як у задач), гейт відповіді reply_leak
(дослівні рядки системного повідомлення у відповіді — відповідь замінюється), гейти конвеєра на пропозицію
і рішення людини на заявку.
"""

import json
import logging
import re
from decimal import Decimal
from uuid import UUID, uuid4

from sandbox.agent import configs, context, gates, outgoing, pipeline
from sandbox.agent.patches import parse_unified_diff
from sandbox.agent.repo import AgentDbUnavailable, cost_summary
from sandbox.agent.settings import (ALLOWED_PATHS, CHAT_HISTORY_TURNS, CHAT_PROMPT, CHAT_PROPOSAL_MAX_CHARS)
from sandbox.core.hashing import sha256_hex

log = logging.getLogger("sandbox.agent.chat")

TEXT_LEAK = "Відповідь приховано: вона дослівно повторювала службові інструкції агента (гейт reply_leak)."
TEXT_BAD_FORMAT = "Модель відповіла не у форматі chat json — показую текст як є, пропозиції немає."
LEAK_MIN_CHARS = 30                 # коротші рядки (заголовки, «Роль:») збігаються випадково
_SECRETS = tuple(re.compile(p) for p in outgoing.BUILTIN_SECRETS)


def _norm(s: str) -> str:
    return " ".join(s.split()).lower()


def gate_reply_leak(reply: str, system: str) -> dict:
    """Чи повторює відповідь дослівно рядки системного повідомлення (промпт, політика, роль)."""
    text = _norm(reply)
    hits = sorted({i for i, line in enumerate(system.splitlines(), 1)
                   if len(_norm(line)) >= LEAK_MIN_CHARS and _norm(line) in text})
    secret = sorted({"secret_pattern" for p in _SECRETS if p.search(reply)})
    return {"gate": "reply_leak", "ok": not hits and not secret, "system_lines": hits[:20], "secrets": secret}


def parse_reply(content: str) -> dict | None:
    """chat json -> {reply, proposal}; None — це не chat json. Модель могла загорнути JSON у ```."""
    raw = pipeline.unfence(content).strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if m is None:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("reply"), str):
        return None
    proposal = data.get("proposal")
    if not isinstance(proposal, str) or not proposal.strip():
        proposal = None
    return {"reply": data["reply"], "proposal": proposal}


def _history_block(turns: list[dict]) -> str:
    if not turns:
        return ""
    body = "".join(f"<turn role=\"user\">{t['user_text']}</turn>\n<turn role=\"assistant\">{t['reply'] or ''}</turn>\n"
                   for t in turns)
    return f"<history>\n{body}</history>\n"


def chat_messages(system: str, message: str, files: list[dict], turns: list[dict]) -> list[dict]:
    """Порядок user-блоків — як у рецепті chat (context.yaml): повідомлення першим (як <task> у задач — модель і
    фейковий провайдер читають намір із першого <task>), далі дозволені файли, файли й історія — лише дані."""
    blocks = "".join(
        f'<site_file path="{f["path"]}" version="{f["version"]}" sha256="{f["sha256"]}">\n'
        f'{f["content"]}</site_file>\n' for f in files)
    return [{"role": "system", "content": system},
            {"role": "user", "content": context._task_block(message) + context._allowed_block() + blocks
             + _history_block(turns)}]


async def submit_proposal(text: str, mode: str, use_cache: bool) -> dict:
    """Єдиний шлях зміни з чату: звичайна задача конвеєра на base=current. Результат — заявка (pending) або
    відмова гейтів; застосувати її може лише людина (approvals.approve з вкладки «Підтвердження»)."""
    code, body = await pipeline.run_task(text[:CHAT_PROPOSAL_MAX_CHARS], mode, "current", use_cache=use_cache)
    if code != 200:
        raise AgentDbUnavailable("proposal")
    task = await pipeline.repo.get_task(UUID(body["task_id"]))
    accounting = {key: float(value) if isinstance(value, Decimal) else value
                  for key, value in cost_summary(dict(c, mode=mode) for c in task["calls"]).items()}
    return {"task_text": body["task_text"], "task_id": body["task_id"], "status": body["status"],
            "message": body["message"], "approval": body["approval"], "gates": body["gates"],
            "cost_usd": body["cost_usd"], "calls": body["calls"], "cache_hits": body["cache_hits"],
            "tokens": body["tokens"], "accounting": accounting}


async def turn(chat_id, message: str, mode: str, use_cache: bool = True,
               overrides: dict | None = None, case_id: str | None = None) -> dict:
    """Один хід. overrides — {path: content} лише для прогону паку ін'єкцій (не з запиту людини): модель бачить
    підкладений файл, а в БД сайту нічого не пишеться."""
    repo = pipeline.repo
    turn_id = uuid4()
    history = await repo.chat_history(chat_id, CHAT_HISTORY_TURNS)
    seq = 1 + max((t["seq"] for t in history), default=0)
    await repo.create_chat_turn(turn_id, chat_id, seq, mode, message, case_id)
    run = pipeline._Run(turn_id, mode, use_cache, insert=repo.insert_chat_call)
    out = {"gates": [], "proposal": None, "error": None, "proposal_text": None}

    async def finish(status: str, reply: str) -> dict:
        total = run.total_cost_usd
        task_id = UUID(out["proposal"]["task_id"]) if out["proposal"] else None
        await repo.finish_chat_turn(turn_id, status=status, reply=reply, proposal_text=out["proposal_text"],
                                    task_id=task_id, gate_report=out["gates"], prompt_tokens=run.prompt_tokens,
                                    completion_tokens=run.completion_tokens, total_cost_usd=total, error=out["error"])
        child = out["proposal"] or {}
        child_cost = child.get("cost_usd", 0)
        own_accounting = cost_summary({"mode": mode, "cost_usd": cost} for cost in run.costs)
        own_accounting.update(provider_attempts=run.calls, cache_hits=run.cache_hits,
                              real_provider_attempts=run.calls if mode == "real" else 0,
                              synthetic_provider_attempts=run.calls if mode != "real" else 0,
                              prompt_tokens=run.prompt_tokens, completion_tokens=run.completion_tokens)
        child_accounting = child.get("accounting") or {}
        accounting = {key: (float(value) if isinstance(value, Decimal) else value) + child_accounting.get(key, 0)
                      for key, value in own_accounting.items()}
        action_cost = None if total is None or child_cost is None else float(total) + child_cost
        return {"turn_id": str(turn_id), "chat_id": str(chat_id), "seq": seq, "mode": mode, "case_id": case_id,
                "status": status, "user_text": message, "reply": reply, "proposal": out["proposal"],
                "proposal_text": out["proposal_text"], "gates": out["gates"], "model": run.model,
                "calls": run.calls, "own_calls": run.calls, "cache_hits": run.cache_hits, "error": out["error"],
                "own_cost_usd": None if total is None else float(total), "action_cost_usd": action_cost,
                "accounting": accounting,
                "action_calls": run.calls + child.get("calls", 0),
                "action_cache_hits": run.cache_hits + child.get("cache_hits", 0),
                "action_tokens": {"prompt": run.prompt_tokens + child.get("tokens", {}).get("prompt", 0),
                                  "completion": run.completion_tokens + child.get("tokens", {}).get("completion", 0)},
                "history": {"limit_turns": CHAT_HISTORY_TURNS, "included_turns": len(history),
                            "truncated": seq > len(history) + 1},
                "tokens": {"prompt": run.prompt_tokens, "completion": run.completion_tokens},
                "cost_usd": None if total is None else float(total), "agent": configs.stamp(agent)}

    agent = configs.pipeline_agent("chat")
    prompt = await pipeline.registry.get_active(CHAT_PROMPT)
    files = await repo.site_files(list(ALLOWED_PATHS), "current")
    if overrides:
        files = [dict(f, content=overrides[f["path"]], sha256=sha256_hex(overrides[f["path"]]))
                 if f["path"] in overrides else f for f in files]
    system = context.system_text(prompt.body, configs.system_extra(agent))
    messages = chat_messages(system, message, files, history)
    res = await run.call("chat", prompt, messages, pipeline.routing.route_task("chat", mode=mode))
    if run.blocked:
        out["error"], out["gates"] = run.blocked["error"], run.blocked["gates"]
        return await finish(run.blocked["status"], run.blocked["message"])
    if not res.ok:
        out["error"] = res.error
        return await finish("provider_error", pipeline.TEXT_PROVIDER_ERROR)

    parsed = parse_reply(res.content)
    out["gates"].append({"gate": "chat_format", "ok": parsed is not None})
    if parsed is None:
        parsed = {"reply": res.content.strip()[:4000] or TEXT_BAD_FORMAT, "proposal": None}
    leak = gate_reply_leak(parsed["reply"], system)
    out["gates"].append(leak)
    if not leak["ok"]:
        # пропозицію з відповіді, що зливає інструкції, теж не беремо: модель явно не тримає політику
        return await finish("blocked_by_output_gate", TEXT_LEAK)
    if parsed["proposal"] is None:
        return await finish("answered", parsed["reply"])

    out["proposal_text"] = parsed["proposal"][:CHAT_PROPOSAL_MAX_CHARS]
    out["proposal"] = await submit_proposal(out["proposal_text"], mode, use_cache)
    status = "proposed" if out["proposal"]["approval"] else "proposal_rejected"
    return await finish(status, parsed["reply"])


async def chat_turn(chat_id, message: str, mode: str, use_cache: bool = True) -> tuple[int, dict]:
    try:
        return 200, await turn(chat_id, message, mode, use_cache)
    except AgentDbUnavailable as exc:
        log.warning("agent chat_id=%s agent db unavailable: %s", chat_id, exc)
        return 503, {"error": "agent_db_unavailable"}


def forbidden_in(diff: str | None) -> list[dict]:
    """Для оцінки паку: заборонені шаблони в diff заявки (гейт мав би її не пропустити)."""
    return gates.gate_forbidden_patterns(parse_unified_diff(diff or "")).get("hits", [])
