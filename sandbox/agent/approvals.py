"""Заявки на застосування diff агента (W4): цикл уроку 8 на агенті.

Модель пропонує -> система створює заявку -> людина вирішує (Approve / Reject) -> система пише нові версії
файлів сайту. Агент сам нічого не застосовує: інструмент apply_patch виконує лише цей модуль і лише за
рішенням людини. Перед записом версії бази звіряються з поточними: хтось змінив файл після пропозиції —
заявка stale (HTTP 409), нічого не пишеться. Запис атомарний і append-only: старі версії не зникають.

Git-проєкт (дія export_patch:<проєкт>): стенд у репозиторій не пише. Approve звіряє sha256 файлів робочої копії з
базою заявки й повторює dry-run; збіглося — заявка applied (схвалено) і відкривається завантаження .patch, який
людина застосовує сама (git apply). База такої заявки — {шлях: sha256}, а не номери версій сайту.
"""

from uuid import uuid4

from sandbox.agent import pipeline, workspace
from sandbox.agent.gates import gate_base_version
from sandbox.agent.patches import PatchParseError, parse_unified_diff
from sandbox.agent.settings import ALLOWED_PATHS
from sandbox.core.hashing import canonical_json, sha256_hex

ACTION = "apply_patch"
EXPORT = "export_patch"
TEXT_EXPORTED = "Схвалено: .patch готовий. Стенд репозиторій не змінював — застосуйте patch у своїй робочій копії."


def export_action(project: str) -> str:
    return f"{EXPORT}:{project}"


def export_project(action: str) -> str | None:
    """Проєкт заявки на .patch або None (заявка на сайт)."""
    kind, _, project = action.partition(":")
    return project or None if kind == EXPORT else None


def key(base_versions: dict, patch_sha256: str, action: str = ACTION) -> str:
    """Ключ ідемпотентності: та сама дія над тими самими версіями тим самим diff — та сама заявка."""
    return sha256_hex(canonical_json({"action": action, "base": base_versions, "patch": patch_sha256}))


async def submit(task_id, diff: str, base_versions: dict, files: list[str], action: str = ACTION) -> dict:
    sha = sha256_hex(diff)
    return await pipeline.repo.create_approval(uuid4(), task_id, key(base_versions, sha, action), base_versions, diff,
                                               sha, files, action)


def summary(a: dict, with_diff: bool = False) -> dict:
    out = {"approval_id": str(a["approval_id"]), "task_id": str(a["task_id"]), "status": a["status"],
           "action": a["action"], "project": export_project(a["action"]), "created_at": a["created_at"].isoformat(),
           "decided_at": a["decided_at"].isoformat() if a["decided_at"] else None,
           "files": a["files"], "base_versions": a["base_versions"], "patch_sha256": a["patch_sha256"],
           "reason": a["reason"], "conflicts": a["conflicts"]}
    if with_diff:
        out["diff"] = a["patch_diff"]
    return out


async def _after(a: dict) -> tuple[list[dict], dict, list[dict]]:
    """(поточні файли заявки, нові тексти після diff, конфлікти версій)."""
    paths = list(a["base_versions"])
    current = await pipeline.repo.site_files(paths, "current")
    conflicts = [{"path": f["path"], "base": a["base_versions"][f["path"]], "current": f["version"]}
                 for f in current if f["version"] != a["base_versions"][f["path"]]]
    report, new = gate_base_version(parse_unified_diff(a["patch_diff"]), current)
    if not report["ok"] and not conflicts:
        conflicts = [{"path": p["path"], "reason": p["reason"]} for p in report["problems"]]
    return current, new, conflicts


def _export_conflicts(a: dict, project: str) -> list[dict]:
    """Файли робочої копії проти бази заявки .patch (sha256) і dry-run того самого diff. Порожньо — можна схвалити."""
    idx = workspace.index(project)
    if idx is None:
        return [{"path": project, "reason": "project_unavailable"}]
    files, conflicts = [], []
    for path, base in a["base_versions"].items():
        text = workspace.read_file(project, path, idx)
        current = None if text is None else sha256_hex(text)
        if current != base:
            conflicts.append({"path": path, "base": base[:12], "current": current and current[:12],
                              **({} if current else {"reason": "missing"})})
        else:
            files.append({"path": path, "version": base, "content": text})
    if conflicts:
        return conflicts
    try:
        report, _ = gate_base_version(parse_unified_diff(a["patch_diff"]), files)
    except PatchParseError as exc:
        return [{"path": ", ".join(a["files"]), "reason": f"bad_patch: {exc}"}]
    return [{"path": p["path"], "reason": p["reason"]} for p in report.get("problems", [])]


async def _approve_export(approval_id, a: dict, project: str) -> tuple[int, dict]:
    conflicts = _export_conflicts(a, project)
    if conflicts:
        await pipeline.repo.close_approval(approval_id, "stale", "base changed", conflicts)
        return 409, {"error": "stale", "message": "Файли репозиторію змінилися після пропозиції: заявку закрито як stale.",
                     "conflicts": conflicts}
    if not await pipeline.repo.close_approval(approval_id, "applied", "approved for .patch export", []):
        return 404, {"error": "not_found", "message": "Заявку вже вирішено."}
    return 200, {"status": "applied", "action": EXPORT, "project": project, "message": TEXT_EXPORTED,
                 "files": [{"path": p, "sha256": s} for p, s in a["base_versions"].items()],
                 "download": f"/agent/approvals/{approval_id}/patch"}


def patch_file(a: dict) -> tuple[int, dict | tuple[str, str]]:
    """(200, (ім'я файлу, текст .patch)) лише для схваленої заявки git-проєкту; інакше (код, помилка)."""
    project = export_project(a["action"])
    if project is None:
        return 404, {"error": "not_found", "message": "Це заявка на сайт: .patch для неї немає."}
    if a["status"] != "applied":
        return 409, {"error": "not_approved", "message": ".patch видається лише після Approve."}
    return 200, (f"{project}-{str(a['approval_id'])[:8]}.patch", a["patch_diff"])


async def approve(approval_id) -> tuple[int, dict]:
    a = await pipeline.repo.get_approval(approval_id)
    if a is None or a["status"] != "pending":
        return 404, {"error": "not_found", "message": "Заявки немає або її вже вирішено."}
    project = export_project(a["action"])
    if project is not None:
        return await _approve_export(approval_id, a, project)
    current, new, conflicts = await _after(a)
    if conflicts:
        await pipeline.repo.close_approval(approval_id, "stale", "base changed", conflicts)
        return 409, {"error": "stale", "message": "Файли змінилися після пропозиції: заявку закрито як stale.",
                     "conflicts": conflicts}
    versions = {f["path"]: f["version"] for f in current}
    new_files = [{"path": p, "version": versions[p] + 1, "content": text, "sha256": sha256_hex(text)}
                 for p, text in new.items()]
    result = await pipeline.repo.apply_approval(approval_id, a["task_id"], new_files)
    if result == "gone":
        return 404, {"error": "not_found", "message": "Заявку вже вирішено."}
    if result == "stale":
        return 409, {"error": "stale", "message": "Файли змінилися під час застосування.", "conflicts": []}
    return 200, {"status": "applied", "files": [{"path": f["path"], "version": f["version"]} for f in new_files]}


async def reject(approval_id, reason: str | None) -> tuple[int, dict]:
    if not await pipeline.repo.close_approval(approval_id, "rejected", reason or "rejected by human", []):
        return 404, {"error": "not_found", "message": "Заявки немає або її вже вирішено."}
    return 200, {"status": "rejected"}


async def reset_site() -> list[str]:
    seed = await pipeline.repo.site_files(list(ALLOWED_PATHS), "seed")
    return await pipeline.repo.reset_site(seed)


async def site_texts(approval_id=None) -> tuple[dict, dict | None]:
    """Тексти файлів сайту: поточні або «після» заявки (dry-run, у БД нічого не пишеться)."""
    files = {f["path"]: f["content"] for f in await pipeline.repo.site_files(list(ALLOWED_PATHS), "current")}
    if approval_id is None:
        return files, None
    a = await pipeline.repo.get_approval(approval_id)
    if a is None or export_project(a["action"]) is not None:
        return files, None                      # заявка .patch стосується репозиторію, а не сайту
    _, new, conflicts = await _after(a)
    return {**files, **new}, {"approval": summary(a), "conflicts": conflicts}


def render(files: dict) -> str:
    """index.html з підставленими include і стилями inline: превʼю віддається з CSP default-src 'none'."""
    html = files.get("demo-site/index.html", "")
    for part in ("header.html", "footer.html"):
        html = html.replace(f"<!-- include: {part} -->", files.get(f"demo-site/{part}", ""))
    css = files.get("demo-site/styles.css", "").replace("</", "<\\/")
    return html.replace('<link rel="stylesheet" href="styles.css">', f"<style>\n{css}</style>")
