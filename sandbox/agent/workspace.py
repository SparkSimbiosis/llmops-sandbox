"""Робочі простори (W6): проєкти з реальними файлами, які агент лише читає.

Джерела: синтетичний тестовий репозиторій у пакеті (`workspaces/notes-app`, публічний) і догфудинг — робоча
копія llm-ops, змонтована в контейнер read-only (`WORKSPACE_LLM_OPS_DIR`, локальний compose-файл). Агент не пише в
жоден із них: тут немає функції запису, а «застосувати» для git-проєкту — це .patch після Approve (repopatch.py),
який людина застосовує сама.

Що агент може побачити, вирішує код, а не модель: білий список шляхів проєкту (include), жорсткі виключення
(секрети, .git, бінарники — для будь-якого проєкту), ліміт розміру файлу й кількості файлів. Шлях із запиту
ніколи не стає шляхом на диску напряму: читати можна лише те, що є в індексі.
"""

import os
import pathlib
import re

from sandbox.core.hashing import canonical_json, sha256_hex
from sandbox.llm.fake_provider import tokens

WORKSPACES_DIR = pathlib.Path(__file__).resolve().parent / "workspaces"
FILE_MAX_BYTES = 64_000
INDEX_MAX_FILES = 400
TEXT_SUFFIXES = {".md", ".txt", ".py", ".js", ".css", ".html", ".json", ".yaml", ".yml", ".toml", ".sql", ".cfg",
                 ".ini", ".sh", ".ps1"}
# виключення для будь-якого проєкту: перевіряються до білого списку, змінити їх конфігурацією проєкту не можна
HARD_EXCLUDE = ("**/.env", "**/.env.*", "**/*.key", "**/*.pem", "**/*.pfx", "**/id_rsa*", "**/*secret*",
                "**/*credential*", "**/auth.json", "**/.git/**", "**/node_modules/**", "**/__pycache__/**",
                "**/.venv/**", "**/*.local.*")

# Deliberately enumerated backend sources: no config/DB/credential subtree glob.
BACKEND_SOURCES = ("sandbox/agent/pipeline.py", "sandbox/agent/chat.py", "sandbox/agent/compiler.py",
                   "sandbox/agent/explore.py", "sandbox/agent/workspace.py", "sandbox/agent/contextpack.py",
                   "sandbox/agent/routing.py", "sandbox/agent/pricing.py", "sandbox/agent/execution.py")
# Course runtime (shop support bot): routing, budget, cache, approvals, guards. Config/DB/provider modules stay out.
COURSE_SOURCES = ("sandbox/main.py", "sandbox/course/pipeline.py", "sandbox/llm/router.py",
                  "sandbox/llm/provider_router.py", "sandbox/llm/budget.py", "sandbox/llm/breaker.py",
                  "sandbox/llm/pricing.py", "sandbox/cache/response_cache.py", "sandbox/cache/semantic.py",
                  "sandbox/approvals/queue.py", "sandbox/tools/runtime.py", "sandbox/core/injection.py",
                  "sandbox/core/pii.py", "sandbox/prompts/registry.py", "sandbox/observability/stats.py")
# Course material only: the stand's own UI, studio configs and working notes (docs/*/) would answer about themselves.
COURSE_DOCS = ("README.md", "GETTING_STARTED.md", "INCIDENT_RUNBOOK.md", "PREREQUISITES.md", "docs/*.md",
               "homework/*.md", "evals/*.md", "evals/*.py", "gateway/litellm-config.yaml", "monitoring/prometheus.yml")

PROJECTS: dict[str, dict] = {
    "notes-app": {
        "title": "notes-app (тестовий репозиторій)", "kind": "sample",
        "about": "синтетичний сервіс нотаток у пакеті стенда: код, тести, ADR, документація",
        "dir": WORKSPACES_DIR / "notes-app", "env": None,
        "include": ("**/*",),
    },
    "llm-ops": {
        "title": "LLM-Ops (догфудинг)", "kind": "mount",
        "about": "репозиторій курсу, змонтований read-only; видно лише білий список: документація, домашки, шлюз, "
                 "евали й бекенд (без UI стенда та робочих нотаток)",
        "dir": None, "env": "WORKSPACE_LLM_OPS_DIR",
        "include": (*COURSE_DOCS, *COURSE_SOURCES, *BACKEND_SOURCES),
    },
}
DEFAULT_PROJECT = "notes-app"


def _glob_re(pattern: str) -> re.Pattern:
    """Glob із ** (будь-яка глибина) -> regex по posix-шляху відносно кореня проєкту."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z", re.I)


_HARD = tuple(_glob_re(p) for p in HARD_EXCLUDE)


def matches(path: str, patterns) -> bool:
    return any(_glob_re(p).match(path) for p in patterns)


def excluded(path: str) -> bool:
    return any(r.match(path) for r in _HARD)


def root(project_id: str) -> pathlib.Path | None:
    p = PROJECTS.get(project_id)
    if p is None:
        return None
    if p["dir"] is not None:
        return p["dir"]
    raw = os.getenv(p["env"] or "", "").strip()
    return pathlib.Path(raw) if raw else None


def status(project_id: str) -> tuple[bool, str | None]:
    r = root(project_id)
    if r is None:
        return False, f"не змонтовано: потрібен {PROJECTS[project_id]['env']} (локальний workspace.local.yml)"
    if not r.is_dir():
        return False, "каталог проєкту недоступний у контейнері"
    return True, None


def projects() -> list[dict]:
    out = []
    for pid, p in PROJECTS.items():
        ok, reason = status(pid)
        out.append({"id": pid, "title": p["title"], "kind": p["kind"], "about": p["about"], "available": ok,
                    "reason": reason, "include": list(p["include"]), "exclude": list(HARD_EXCLUDE)})
    return out


def _kind(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if path.startswith("tests/") or "/tests/" in path or name.startswith("test_"):
        return "test"
    if name.endswith((".md", ".txt")):
        return "doc"
    if name.endswith((".yaml", ".yml", ".toml", ".json", ".cfg", ".ini")):
        return "config"
    return "code"


def _read(base: pathlib.Path, rel: str) -> str | None:
    p = base / rel
    try:
        if p.is_symlink() or not p.resolve().is_relative_to(base.resolve()):
            return None                         # симлінк міг би вивести за межі проєкту
        return p.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def index(project_id: str) -> dict | None:
    """Знімок проєкту: файли (шлях, розмір, рядки, токени, sha256, вид), пропущені з причиною і sha256 знімка.
    None — немає такого проєкту або він недоступний."""
    if project_id not in PROJECTS or not status(project_id)[0]:
        return None
    base = root(project_id)
    include = PROJECTS[project_id]["include"]
    files, skipped = [], []
    outside_include = 0
    # порядок за posix-шляхом (а не за Path: на Windows він без урахування регістру) — знімок однаковий на всіх ОС
    for rel, p in sorted((p.relative_to(base).as_posix(), p) for p in base.rglob("*") if p.is_file()):
        if excluded(rel):
            # не показуємо навіть імені файлу з виключених каталогів (.git, node_modules) — лише секретоподібні
            if not any(part in rel for part in (".git/", "node_modules/", "__pycache__/", ".venv/")):
                skipped.append({"path": rel, "reason": "hard_exclude"})
            continue
        if not matches(rel, include):
            outside_include += 1
            continue
        if p.suffix.lower() not in TEXT_SUFFIXES:
            skipped.append({"path": rel, "reason": "not_text"})
            continue
        if p.stat().st_size > FILE_MAX_BYTES:
            skipped.append({"path": rel, "reason": "too_big"})
            continue
        if len(files) >= INDEX_MAX_FILES:
            skipped.append({"path": rel, "reason": "index_limit"})
            continue
        text = _read(base, rel)
        if text is None:
            skipped.append({"path": rel, "reason": "unreadable"})
            continue
        files.append({"path": rel, "bytes": len(text.encode("utf-8")), "lines": text.count("\n") + 1,
                      "tokens": tokens(text), "sha256": sha256_hex(text), "kind": _kind(rel)})
    snapshot = sha256_hex(canonical_json([[f["path"], f["sha256"]] for f in files]))
    return {"project": project_id, "title": PROJECTS[project_id]["title"], "files": files, "skipped": skipped,
            "snapshot_sha256": snapshot, "tokens": sum(f["tokens"] for f in files),
            "coverage": {"scope": "allowlisted_files_only", "include": list(include),
                         "hard_exclude": list(HARD_EXCLUDE), "outside_include_files": outside_include,
                         "indexed_files": len(files), "skipped_files": len(skipped)}}


def read_file(project_id: str, path: str, idx: dict | None = None) -> str | None:
    """Текст файлу, лише якщо він є в індексі проєкту (білий список уже застосовано)."""
    idx = idx or index(project_id)
    if idx is None or idx["project"] != project_id:
        return None
    entry = next((f for f in idx["files"] if f["path"] == path), None)
    if entry is None:
        return None
    text = _read(root(project_id), path)
    # A stale index must never label newly read content with the old snapshot hash.
    return text if text is not None and sha256_hex(text) == entry["sha256"] else None
