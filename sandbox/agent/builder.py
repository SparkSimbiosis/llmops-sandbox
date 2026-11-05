"""Конструктор промптів стенда: рушій + методика + контракт + поля чернетки -> системний промпт.

Дані — YAML у studio/ (engines, contracts, scenario; роутинг — у routing.py над тим самим каталогом).
Логіка за зразком вихідного конструктора рушіїв: обов'язкові поля методики, оцінка якості чернетки (0..100),
прапорці ризику за ключовими словами, рендер розділами. Нічого не зберігає сам: зберегти зібраний
промпт як нову версію agent-editor — окремий маршрут через реєстр промптів.
"""

import pathlib

import yaml

from sandbox.agent import routing
from sandbox.agent.settings import PROMPT_MAX_CHARS
from sandbox.core.hashing import sha256_hex

STUDIO_DIR = pathlib.Path(__file__).resolve().parent / "studio"


def _load(name: str) -> dict:
    return yaml.safe_load((STUDIO_DIR / name).read_text(encoding="utf-8"))


_ENGINES_DOC = _load("engines.yaml")
ENGINES: dict[str, dict] = _ENGINES_DOC["engines"]
# як approve у вихідному конструкторі: чернетку нижче порогу зібрати можна, зберегти версією — ні
APPROVE_THRESHOLD: int = _ENGINES_DOC["approve_threshold"]
CONTRACTS: dict[str, dict] = _load("contracts.yaml")["contracts"]
SCENARIO: dict = _load("scenario.yaml")["scenario"]
_EXAMPLES_DOC = _load("engine-examples.yaml")
EXAMPLES: dict[str, dict] = _EXAMPLES_DOC["examples"]
EXAMPLE_PROJECTS: dict[str, str] = _EXAMPLES_DOC["example_projects"]

DRAFT_TEXT_FIELDS = ("title", "task_goal", "background_context", "input_data")
DRAFT_LIST_FIELDS = ("constraints", "acceptance_criteria")

BROAD_WORDS = ("все", "весь", "усе", "повністю", "полностью", "кожен", "каждый", "all", "completely")
# ключові слова -> які прапорці методики вони вмикають
RISK_WORDS = {
    ("db", "баз", "sql", "schema"): ("db_connection_leak", "invariant_violation"),
    ("зміни", "измен", "рефактор", "change", "refactor"): ("breaking_change", "regression_risk"),
    ("script", "скрипт", "token", "токен", "пароль", "password"): ("security_flaw", "resource_leak"),
}


def catalog() -> dict:
    """Усе, що бачить вкладка «Конструктор»: сценарій, рушії, картки контрактів і таблиця роутингу."""
    return {"scenario": SCENARIO, "approve_threshold": APPROVE_THRESHOLD,
            "engines": [{"id": k, **v, "example": EXAMPLES[k], "example_project": EXAMPLE_PROJECTS[k]} for k, v in ENGINES.items()],
            "contracts": [{"id": k, **v} for k, v in CONTRACTS.items()],
            "routing": {"classes": routing.CLASSES, "rules": routing.RULES, "steps": routing.TABLE["steps"],
                        "long_task_chars": routing.LONG_TASK_CHARS}}


def _clean(draft: dict) -> dict:
    out = {f: str(draft.get(f) or "").strip() for f in DRAFT_TEXT_FIELDS}
    for f in DRAFT_LIST_FIELDS:
        out[f] = [str(x).strip() for x in (draft.get(f) or []) if str(x).strip()]
    out["complexity"] = draft.get("complexity") if draft.get("complexity") in (routing.SIMPLE, routing.COMPLEX) else None
    return out


def review(engine: dict, d: dict) -> dict:
    """Оцінка чернетки: чого бракує, які ризики, що порадити. Не блокує збірку — лише підказує."""
    missing, recs, score = [], [], 100
    if len(d["title"]) < 5:
        missing.append("title"); score -= 10
        recs.append("Заголовок — щонайменше 5 символів.")
    if len(d["task_goal"]) < 10:
        missing.append("task_goal"); score -= 15
        recs.append("Мета задачі — щонайменше 10 символів.")
    for f in engine["methodology"]["required_fields"]:
        if not d.get(f) and f not in missing:
            missing.append(f); score -= 15
            recs.append(f"Методика вимагає поле '{f}'.")
    if not d["constraints"]:
        score -= 10
        recs.append("Додайте хоча б одне обмеження.")
    if len(d["acceptance_criteria"]) < 2:
        if "acceptance_criteria" not in missing:
            missing.append("acceptance_criteria")
        score -= 10 if d["acceptance_criteria"] else 15
        recs.append("Потрібно щонайменше 2 критерії приймання.")

    text = f"{d['title']} {d['task_goal']}".lower()
    flags = []
    if any(w in text.split() for w in BROAD_WORDS):
        flags.append("broad_scope_risk")
        recs.append("Задача сформульована надто широко — розбийте її на менші кроки.")
    allowed = engine["methodology"]["risk_flags"]
    for words, risk in RISK_WORDS.items():
        if any(w in text for w in words):
            flags += [r for r in risk if r in allowed and r not in flags]
    return {"quality_score": max(0, min(100, score)), "missing_fields": missing, "risk_flags": flags,
            "recommendations": recs}


SECTION_TITLES = {"title": "ЗАДАЧА", "task_goal": "МЕТА", "background_context": "КОНТЕКСТ", "input_data": "ВХІДНІ ДАНІ",
                  "constraints": "ОБМЕЖЕННЯ", "acceptance_criteria": "КРИТЕРІЇ ПРИЙМАННЯ"}


def _value(d: dict, field: str) -> str:
    v = d[field]
    if isinstance(v, list):
        return "\n".join(f"- {x}" for x in v) or "—"
    return v or "—"


def render(engine: dict, d: dict) -> str:
    """Порядок, як у вихідному конструкторі: пак рушія (шаблон зі змінними) -> поля, яких немає в шаблоні ->
    методика -> контракт виходу. Поле, вже підставлене в шаблон, окремим розділом не дублюємо."""
    m = engine["methodology"]
    contract = CONTRACTS[engine["contract"]]
    template = engine.get("template") or {"variables": [], "body": ""}
    used = set(template["variables"])
    parts = [engine["system_prompt"]]
    if template["body"]:
        parts.append(template["body"].format_map({f: _value(d, f) for f in used}).strip())
    for field, title in SECTION_TITLES.items():
        if field not in used and d[field]:
            parts.append(f"### {title}\n{_value(d, field)}")
    parts.append("### МЕТОДИКА\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(m["steps"], 1)))
    parts.append(f"### КОНТРАКТ ВИХОДУ ({engine['contract']})\n{contract['output']}")
    return "\n\n".join(parts)


def build(engine_id: str, draft: dict) -> dict | None:
    """None — такого рушія немає. Роутинг рахує routing.decide: той самий, що й у конвеєрі."""
    engine = ENGINES.get(engine_id)
    if engine is None:
        return None
    d = _clean(draft)
    rev = review(engine, d)
    prompt = render(engine, d)
    task_chars = len(d["title"]) + len(d["task_goal"])
    route = routing.decide(d["complexity"], task_chars, rev["risk_flags"])
    result = {"engine": engine_id, "target_prompt": engine["target_prompt"], "contract": engine["contract"],
              "methodology": engine["methodology"]["id"], "draft": d, "review": rev, "route": route,
              "prompt": prompt, "sha256": sha256_hex(prompt), "chars": len(prompt),
              "too_long": len(prompt) > PROMPT_MAX_CHARS,
              "approvable": rev["quality_score"] >= APPROVE_THRESHOLD, "approve_threshold": APPROVE_THRESHOLD}
    result["yaml"] = to_yaml(result)
    return result


def to_yaml(result: dict) -> str:
    """Знімок збірки в YAML: можна зберегти файлом і відтворити промпт тим самим рушієм."""
    doc = {k: result[k] for k in ("engine", "methodology", "contract", "target_prompt", "draft", "route",
                                  "review", "sha256", "prompt")}
    return yaml.safe_dump({"version": 1, "built_prompt": doc}, allow_unicode=True, sort_keys=False, width=100)
