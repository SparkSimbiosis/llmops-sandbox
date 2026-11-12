"""Реєстр інструкцій стенда (I-1): ролі, правила, workflow, контракти, skills з версіями.

Джерела лише для читання: синтетичний приклад у пакеті (`instructions_sample/`) або локальний каталог інструкцій,
змонтований read-only (`INSTRUCTIONS_DIR`). Каталог не змінюється; у БД — текст, sha256 і коміт джерела.
Версія = vN за порядком імпорту; той самий текст нової версії не створює (як редактор промптів).
"""

import logging
import os
import pathlib
import re
from dataclasses import dataclass

from sandbox.core.hashing import sha256_hex

log = logging.getLogger("sandbox.agent.instructions")

KINDS = ("role", "rule", "workflow", "contract", "skill")
SAMPLE_DIR = pathlib.Path(__file__).resolve().parent / "instructions_sample"

MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 200
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


@dataclass(frozen=True)
class Found:
    kind: str
    name: str
    path: str       # відносно кореня джерела, з прямими слешами
    body: str


def local_dir() -> pathlib.Path | None:
    raw = os.getenv("INSTRUCTIONS_DIR", "").strip()
    if not raw:
        return None
    p = pathlib.Path(raw)
    return p if p.is_dir() else None


def parse_overrides(raw: str | None) -> dict[str, str]:
    """INSTRUCTIONS_KINDS="policy/MODES.md=workflow,policy/TRACE.md=contract": перевизначити тип окремих файлів.
    Невідомий тип або кривий запис пропускаємо з WARN — імпорт не падає через оточення."""
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        if not part.strip():
            continue
        path, _, kind = part.partition("=")
        if kind.strip() in KINDS and path.strip():
            out[path.strip().replace("\\", "/")] = kind.strip()
        else:
            log.warning("instruction kind override ignored: %r", part)
    return out


def _layout(rel: str) -> tuple[str, str] | None:
    """Тип і ім'я за розкладкою каталогу; None — файл не є інструкцією."""
    parts = rel.split("/")
    if len(parts) == 2 and parts[1].endswith(".md"):
        stem = parts[1][:-3]
        kind = {"agents": "role", "policy": "rule", "workflows": "workflow", "contracts": "contract"}.get(parts[0])
        return (kind, stem) if kind else None
    if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md":
        return "skill", parts[1]
    return None


def scan(root: pathlib.Path, overrides: dict[str, str] | None = None) -> list[Found]:
    """Лише .md за розкладкою, без symlink, у межах кореня, з лімітами на розмір і кількість."""
    overrides = overrides or {}
    root = root.resolve()
    found: list[Found] = []
    for p in sorted(root.rglob("*.md")):
        if p.is_symlink() or ".git" in p.parts:
            continue
        try:
            real = p.resolve()
            real.relative_to(root)                 # шлях не виходить за межі джерела
        except (OSError, ValueError):
            continue
        rel = real.relative_to(root).as_posix()
        hit = _layout(rel)
        if hit is None:
            continue
        kind, name = hit
        kind = overrides.get(rel, kind)
        if not _NAME.match(name):
            log.warning("instruction skipped, bad name: %s", rel)
            continue
        if real.stat().st_size > MAX_FILE_BYTES:
            log.warning("instruction skipped, too large: %s", rel)
            continue
        found.append(Found(kind, name, rel, real.read_text(encoding="utf-8", errors="replace")))
        if len(found) >= MAX_FILES:
            log.warning("instruction import stopped at %d files", MAX_FILES)
            break
    return found


def git_ref(root: pathlib.Path) -> str | None:
    """Коміт HEAD джерела без git у контейнері: .git/HEAD -> ref -> sha (або packed-refs). Не git — None."""
    git = root / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head if re.fullmatch(r"[0-9a-f]{40}", head) else None
        ref = head[5:]
        loose = git / ref
        if loose.is_file():
            return loose.read_text(encoding="utf-8").strip() or None
        for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
            sha, _, name = line.partition(" ")
            if name == ref:
                return sha
    except OSError:
        return None
    return None


class InstructionRegistry:
    def __init__(self, repo):
        self._repo = repo

    async def import_from(self, source: str, root: pathlib.Path, overrides: dict[str, str] | None = None) -> dict:
        ref = git_ref(root)
        items = []
        for f in scan(root, overrides):
            version, created = await self._repo.import_instruction(
                f.kind, f.name, f.body, sha256_hex(f.body), source, f.path, ref)
            items.append({"kind": f.kind, "name": f.name, "version": version, "created": created, "path": f.path})
        return {"source": source, "source_ref": ref, "imported": len(items),
                "created": sum(i["created"] for i in items), "items": items}

    async def list(self, project: str) -> "list[dict]":   # list тут — метод класу
        return [{"kind": r["kind"], "name": r["name"], "versions": r["versions"], "latest": r["latest"],
                 "active_version": r["active_version"], "share_with_model": bool(r["share_with_model"]),
                 "source": r["source"], "source_path": r["source_path"]}
                for r in await self._repo.list_instructions(project)]

    async def versions(self, kind: str, name: str) -> "list[dict]":
        return [{"version": r["version"], "sha256": r["sha256"], "source": r["source"], "source_ref": r["source_ref"],
                 "imported_at": r["imported_at"].isoformat()}
                for r in await self._repo.instruction_versions(kind, name)]

    async def get(self, kind: str, name: str, version: str) -> dict | None:
        r = await self._repo.get_instruction(kind, name, version)
        if r is None:
            return None
        return {"kind": kind, "name": name, "version": version, "sha256": r["sha256"], "body": r["body"],
                "source": r["source"], "source_path": r["source_path"], "source_ref": r["source_ref"]}

    async def activate(self, project: str, kind: str, name: str, version: str) -> dict | None:
        """None — такої версії немає (маршрут відповідає 404)."""
        previous = await self._repo.activate_instruction(project, kind, name, version)
        if previous is False:
            return None
        return {"project": project, "kind": kind, "name": name, "version": version, "previous_version": previous}

    async def share(self, project: str, kind: str, name: str, share: bool) -> dict | None:
        """None — для проєкту немає активної версії (маршрут відповідає 409): ділитися нема чим."""
        if not await self._repo.set_instruction_share(project, kind, name, share):
            return None
        return {"project": project, "kind": kind, "name": name, "share_with_model": share}
