"""Конфігурація агентів стенда (C-1): політика, агенти, ролі, скіли, інструменти, рецепти контексту — файлами в studio/.

Політика (policy.yaml + policy/base.md) — шар над агентами: текст base.md іде в system кожного агента одразу після
версіонованого промпта, а кожне активне правило з policy.yaml назване разом із кодом, що його забезпечує.

Усе читається при імпорті з YAML/MD (без БД і без міграцій), а validate() перевіряє посилання між файлами:
агент -> роль, скіли, інструменти, рецепт, контракт, рушій, клас моделі. Тексти ролей і скілів ідуть у
системне повідомлення кроку конвеєра поруч із версіонованим промптом; формат відповіді (JSON-план, unified
diff) задає лише промпт із реєстру — тому роль і скіл не можуть містити маркерів формату, інакше «зламаний»
промпт (W1, вкладка «Зламати») перестав би ламати крок.
"""

import importlib
import pathlib

import yaml

from sandbox.agent import builder, routing
from sandbox.agent.settings import CHAT_PROMPT, EDITOR_PROMPT, EXPLORE_PROMPT, PLANNER_PROMPT
from sandbox.core.hashing import sha256_hex

STUDIO_DIR = builder.STUDIO_DIR
# маркери формату, за якими (фейкова) модель розуміє, що відповідати: живуть лише у версіонованому промпті
FORMAT_MARKERS = ("json plan", "unified diff", "chat json", "explore json")
PIPELINE_PROMPTS = {"plan": PLANNER_PROMPT, "propose": EDITOR_PROMPT, "chat": CHAT_PROMPT, "explore": EXPLORE_PROMPT}
FILE_KINDS = {"policy.yaml": "policy", "agents.yaml": "agents", "tools.yaml": "tools", "context.yaml": "context", "engines.yaml": "engines",
              "contracts.yaml": "contracts", "routing.yaml": "routing", "scenario.yaml": "scenario",
              "injections.yaml": "injections"}


def _load(name: str) -> dict:
    return yaml.safe_load((STUDIO_DIR / name).read_text(encoding="utf-8"))


def _front(path: pathlib.Path) -> dict:
    """Markdown з front matter (--- name/description ---) -> {name, description, body, path, sha256}."""
    text = path.read_text(encoding="utf-8")
    meta, body = {}, text
    if text.startswith("---\n"):
        head, _, body = text[4:].partition("\n---\n")
        meta = yaml.safe_load(head) or {}
    body = body.strip()
    return {"name": meta.get("name", path.stem), "description": meta.get("description", ""), "body": body,
            "path": path.relative_to(STUDIO_DIR).as_posix(), "sha256": sha256_hex(body)}


AGENTS: dict[str, dict] = _load("agents.yaml")["agents"]
TOOLS: dict[str, dict] = _load("tools.yaml")["tools"]
_CONTEXT = _load("context.yaml")
BLOCKS: dict[str, dict] = _CONTEXT["blocks"]
RECIPES: dict[str, dict] = _CONTEXT["recipes"]
ROLES: dict[str, dict] = {r["name"]: r for r in (_front(p) for p in sorted((STUDIO_DIR / "roles").glob("*.md")))}
SKILLS: dict[str, dict] = {s["name"]: s for s in (_front(p) for p in sorted((STUDIO_DIR / "skills").glob("*/SKILL.md")))}
POLICY: dict = _load("policy.yaml")
POLICY_DOC: dict = _front(STUDIO_DIR / POLICY["instruction"])


def resolve(ref: str) -> bool:
    """Чи існує sandbox.x.y.attr[.attr] — так validate() перевіряє, що правило справді чимось забезпечене."""
    parts = ref.split(".")
    for i in range(len(parts) - 1, 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
        except ImportError:
            continue
        for name in parts[i:]:
            if not hasattr(obj, name):
                return False
            obj = getattr(obj, name)
        return True
    return False


def validate() -> list[str]:
    """Порожній список — конфігурація цілісна. Інакше — що саме зламано (для тестів і для UI)."""
    errors = []
    for aid, a in AGENTS.items():
        where = f"agents.{aid}"
        if a.get("role") not in ROLES:
            errors.append(f"{where}: немає ролі '{a.get('role')}' у roles/")
        errors += [f"{where}: немає скіла '{s}' у skills/" for s in a.get("skills", []) if s not in SKILLS]
        errors += [f"{where}: немає інструмента '{t}' у tools.yaml" for t in a.get("tools", []) if t not in TOOLS]
        if a.get("context") not in RECIPES:
            errors.append(f"{where}: немає рецепта '{a.get('context')}' у context.yaml")
        if a.get("contract") not in builder.CONTRACTS:
            errors.append(f"{where}: немає контракту '{a.get('contract')}' у contracts.yaml")
        if a.get("engine") is not None and a["engine"] not in builder.ENGINES:
            errors.append(f"{where}: немає рушія '{a['engine']}' у engines.yaml")
        cls = (a.get("model") or {}).get("class")
        if cls is not None and cls not in routing.CLASSES:
            errors.append(f"{where}: немає класу моделі '{cls}' у routing.yaml")
        if a.get("pipeline") and a.get("step") not in PIPELINE_PROMPTS:
            errors.append(f"{where}: pipeline-агент має бути кроком plan, propose, chat або explore")
        elif a.get("pipeline") and a.get("prompt") != PIPELINE_PROMPTS[a["step"]]:
            # конвеєр бере промпт кроку з settings — компілятор мусить показувати той самий
            errors.append(f"{where}: промпт '{a.get('prompt')}' ≠ промпт кроку конвеєра '{PIPELINE_PROMPTS[a['step']]}'")
    for rid, r in RECIPES.items():
        for part in ("system", "user"):
            errors += [f"context.recipes.{rid}.{part}: невідомий блок '{b}'" for b in r.get(part, []) if b not in BLOCKS]
    for eid, e in builder.ENGINES.items():
        if e.get("role") not in ROLES:
            errors.append(f"engines.{eid}: немає ролі '{e.get('role')}' у roles/")
    for rid, rule in POLICY["rules"].items():
        where = f"policy.{rid}"
        if rule.get("layer") not in POLICY["layers"]:
            errors.append(f"{where}: невідомий шар '{rule.get('layer')}'")
        if rule.get("status") == "active" and not rule.get("code"):
            errors.append(f"{where}: активне правило без коду, що його забезпечує")
        if rule.get("code") and not resolve(rule["code"]):
            errors.append(f"{where}: немає '{rule['code']}'")
        if rule.get("config") and not (STUDIO_DIR / rule["config"]).exists():
            errors.append(f"{where}: немає файлу studio/{rule['config']}")
    in_system = {a.get("contract") for a in AGENTS.values()
                 if "contract" in RECIPES.get(a.get("context"), {}).get("system", [])}
    for cid in in_system & set(builder.CONTRACTS):
        low = builder.CONTRACTS[cid]["output"].lower()
        # картка контракту йде в system (блок contract): маркер формату в ній зробив би зламаний промпт робочим
        errors += [f"contracts.{cid}: маркер формату '{m}' — він лише у версіонованому промпті"
                   for m in FORMAT_MARKERS if m in low]
    for kind, items in (("policy", {"base": POLICY_DOC}), ("roles", ROLES), ("skills", SKILLS)):
        for name, item in items.items():
            low = item["body"].lower()
            errors += [f"{kind}/{name}: маркер формату '{m}' — він лише у версіонованому промпті"
                       for m in FORMAT_MARKERS if m in low]
    return errors


def system_extra(agent_id: str) -> str:
    """Політика, роль і скіли агента — додаток до системного повідомлення кроку (порядок — як у context.yaml)."""
    a = AGENTS[agent_id]
    parts = [POLICY_DOC["body"], ROLES[a["role"]]["body"]] + [f"Скіл {s}:\n{SKILLS[s]['body']}" for s in a.get("skills", [])]
    return "\n\n".join(parts)


def stamp(agent_id: str) -> dict:
    """Які версії конфігурації пішли у виклик: політика, роль і скіли за sha256 тексту."""
    a = AGENTS[agent_id]
    return {"agent": agent_id, "policy_sha256": POLICY_DOC["sha256"], "role": a["role"], "role_sha256": ROLES[a["role"]]["sha256"],
            "skills": [{"name": s, "sha256": SKILLS[s]["sha256"]} for s in a.get("skills", [])]}


def pipeline_agent(step: str) -> str:
    return next(aid for aid, a in AGENTS.items() if a.get("pipeline") and a.get("step") == step)


def files() -> list[dict]:
    """Дерево файлів конфігурації з текстом і sha256 — як воно лежить у репозиторії."""
    out = []
    for p in sorted(STUDIO_DIR.rglob("*")):
        if not p.is_file() or p.suffix not in (".yaml", ".md"):
            continue
        rel = p.relative_to(STUDIO_DIR).as_posix()
        text = p.read_text(encoding="utf-8")
        kind = FILE_KINDS.get(rel) or rel.split("/")[0]
        out.append({"path": f"sandbox/agent/studio/{rel}", "name": rel, "kind": kind,
                    "chars": len(text), "sha256": sha256_hex(text), "text": text})
    return out


def policy() -> dict:
    rules = [{"id": k, **v, "code_ok": bool(v.get("code")) and resolve(v["code"])} for k, v in POLICY["rules"].items()]
    return {"layers": POLICY["layers"], "rules": rules, "instruction": POLICY_DOC}


def catalog() -> dict:
    engines = [{"id": k, "title": e["title"], "role": e["role"], "model_class": e["model_class"],
                "contract": e["contract"], "target_prompt": e.get("target_prompt"),
                "variables": e["template"]["variables"]} for k, e in builder.ENGINES.items()]
    return {"policy": policy(), "prompts": PIPELINE_PROMPTS, "engines": engines, "agents": [{"id": k, **v, "role_description": ROLES.get(v.get("role"), {}).get("description")}
                       for k, v in AGENTS.items()],
            "roles": list(ROLES.values()), "skills": list(SKILLS.values()),
            "tools": [{"id": k, **v} for k, v in TOOLS.items()],
            "blocks": BLOCKS, "recipes": RECIPES, "files": files(), "errors": validate()}
