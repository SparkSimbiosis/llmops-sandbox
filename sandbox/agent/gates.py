"""Гейти пропозиції агента. hw1: parse_ok (це справді diff) і allowed_paths (лише файли зі списку).
W4: forbidden_patterns (у доданих рядках немає скриптів, inline-обробників і зовнішніх URL), size_limits
(файли, змінені рядки, байти) і base_version (diff застосовується до версій файлів, які бачила модель).

.patch для git-проєкту (run_repo_gates): дозволені лише файли пакета контексту, а замість правил сайту —
secrets (секрети й приватні імена в доданих рядках).

Кожен гейт повертає {"gate": ..., "ok": ...}; хоч один ok:false -> статус rejected_by_gate.
"""

import re

from sandbox.agent.patches import NULL_PATH, ParsedPatch, PatchApplyError, PatchParseError, apply_file, parse_unified_diff
from sandbox.agent.settings import ALLOWED_PATHS, PATCH_MAX_BYTES, PATCH_MAX_CHANGED_LINES, PATCH_MAX_FILES

_DRIVE = re.compile(r"^[A-Za-z]:")


def _shown(path: str) -> str:
    # NUL не можна покласти ні в jsonb, ні в text Postgres — показуємо його екранованим
    return path.replace("\x00", "\\x00")


def path_problem(path: str, allowed=ALLOWED_PATHS) -> str | None:
    """Причина, чому шлях не можна змінювати, або None. Порядок перевірок — від найгрубіших:
    навіть якщо шлях випадково збігся б зі списком, NUL чи '..' його не пропустять."""
    if not path:
        return "empty"
    if "\x00" in path:
        return "nul_byte"
    if "\\" in path:
        return "backslash"
    if path == NULL_PATH:
        return "create_or_delete"
    if path.startswith("/") or _DRIVE.match(path):
        return "absolute"
    if any(part in ("..", ".") for part in path.split("/")):
        return "dot_segment"
    if path not in allowed:
        return "not_allowed"
    return None


def gate_parse_ok(text: str, recount: bool = False) -> tuple[dict, ParsedPatch | None]:
    try:
        patch = parse_unified_diff(text, recount)
    except PatchParseError as exc:
        return {"gate": "parse_ok", "ok": False, "error": str(exc)}, None
    return {"gate": "parse_ok", "ok": True, **({"recounted": True} if patch.recounted else {})}, patch


def gate_allowed_paths(patch: ParsedPatch | None, allowed=ALLOWED_PATHS) -> dict:
    if patch is None:
        # без розбору шляхи невідомі — гейт закритий, а не «пропущений»
        return {"gate": "allowed_paths", "ok": False, "error": "not checked: diff did not parse"}
    violations = []
    for f in patch.files:
        if f.old_path != f.new_path:
            violations.append({"path": _shown(f.new_path), "reason": "rename"})
        for path in dict.fromkeys((f.old_path, f.new_path)):
            reason = path_problem(path, allowed)
            if reason:
                violations.append({"path": _shown(path), "reason": reason})
    if violations:
        return {"gate": "allowed_paths", "ok": False, "violations": violations}
    return {"gate": "allowed_paths", "ok": True}


# що не можна додавати на сайт: перевіряються лише додані рядки diff (видаляти таке можна)
FORBIDDEN = (
    ("script_tag", re.compile(r"<\s*script\b", re.I)),
    ("embed_tag", re.compile(r"<\s*(iframe|object|embed)\b", re.I)),
    ("inline_handler", re.compile(r"\son[a-z]+\s*=", re.I)),
    ("javascript_url", re.compile(r"javascript\s*:", re.I)),
    ("external_url", re.compile(r"(https?:)?//[a-z0-9.-]+\.[a-z]{2,}", re.I)),
    ("css_import", re.compile(r"@import\b", re.I)),
    ("nul_byte", re.compile("\x00")),      # Postgres не зберігає NUL у text: такий файл не записати
)


def _closed(gate: str) -> dict:
    # без розбору патча перевіряти нічого — гейт закритий, а не «пропущений»
    return {"gate": gate, "ok": False, "error": "not checked: diff did not parse"}


def _added(patch: ParsedPatch):
    """(шлях, номер рядка в новому файлі, текст) кожного доданого рядка."""
    for f in patch.files:
        for h in f.hunks:
            new_line = h.new_start
            for line in h.lines:
                if line.startswith("+"):
                    yield _shown(f.new_path), new_line, line[1:]
                if line.startswith((" ", "+")):
                    new_line += 1


def gate_forbidden_patterns(patch: ParsedPatch | None) -> dict:
    if patch is None:
        return _closed("forbidden_patterns")
    hits = [{"path": path, "line": n, "rule": rule}
            for path, n, text in _added(patch) for rule, rx in FORBIDDEN if rx.search(text)]
    if hits:
        return {"gate": "forbidden_patterns", "ok": False, "hits": hits}
    return {"gate": "forbidden_patterns", "ok": True}


def gate_secrets(patch: ParsedPatch | None, rules) -> dict:
    """.patch іде в робочу копію людини: у доданих рядках не має бути секретів (вбудовані шаблони + локальні) і
    приватних імен з локального denylist. Текст збігу не показуємо. Немає denylist — секрети все одно перевіряються,
    а звіт чесно каже, що імена не перевірено."""
    if patch is None:
        return _closed("secrets")
    hits = []
    for path, n, text in _added(patch):
        if "\x00" in text:
            hits.append({"path": path, "line": n, "rule": "nul_byte"})
        if any(p.search(text) for p in rules.secrets):
            hits.append({"path": path, "line": n, "rule": "secret_pattern"})
        if any(p.search(text) for p in rules.denylist):
            hits.append({"path": path, "line": n, "rule": "denylist"})
    report = {"gate": "secrets", "ok": not hits, "denylist": "checked" if rules.ready else "not_configured"}
    return {**report, "hits": hits} if hits else report


def gate_size_limits(text: str, patch: ParsedPatch | None) -> dict:
    if patch is None:
        return _closed("size_limits")
    size = {"files": len(patch.files), "changed_lines": patch.added + patch.removed,
            "bytes": len(text.encode("utf-8", "surrogatepass"))}
    limits = {"files": PATCH_MAX_FILES, "changed_lines": PATCH_MAX_CHANGED_LINES, "bytes": PATCH_MAX_BYTES}
    over = [k for k in limits if size[k] > limits[k]]
    report = {"gate": "size_limits", "ok": not over, **size}
    if over:
        report["over"] = {k: limits[k] for k in over}
    return report


def gate_base_version(patch: ParsedPatch | None, files: list[dict]) -> tuple[dict, dict[str, str]]:
    """Dry-run: diff застосовується до тих самих версій файлів, що були в контексті. Повертає звіт і
    нові тексти файлів (порожньо, якщо не застосувався)."""
    if patch is None:
        return _closed("base_version"), {}
    by_path = {f["path"]: f for f in files}
    result, problems = {}, []
    for fp in patch.files:
        base = by_path.get(fp.new_path)
        if base is None:
            problems.append({"path": _shown(fp.new_path), "reason": "not_in_context"})
            continue
        try:
            result[fp.new_path] = apply_file(result.get(fp.new_path, base["content"]), fp)
        except PatchApplyError as exc:
            problems.append({"path": fp.new_path, "reason": "does_not_apply", "error": str(exc)})
    versions = {p: by_path[p]["version"] for p in result}
    if problems:
        return {"gate": "base_version", "ok": False, "problems": problems}, {}
    return {"gate": "base_version", "ok": True, "versions": versions}, result


def run_gates(text: str, files: list[dict] | None = None) -> tuple[list[dict], ParsedPatch | None]:
    """files — файли контексту: з ними додається dry-run base_version (без них — лише статичні гейти)."""
    parse_report, patch = gate_parse_ok(text)
    reports = [parse_report, gate_allowed_paths(patch), gate_forbidden_patterns(patch), gate_size_limits(text, patch)]
    if files is not None:
        reports.append(gate_base_version(patch, files)[0])
    return reports, patch


def run_repo_gates(text: str, files: list[dict], rules) -> tuple[list[dict], ParsedPatch | None, dict[str, str]]:
    """Гейти .patch git-проєкту. files — файли пакета контексту ({path, content, version=sha256}): лише їх модель
    бачила, тож лише їх і можна змінювати; dry-run іде на їхньому повному поточному тексті. Повертає звіти, розібраний
    patch і нові тексти файлів (порожньо, якщо хоч один гейт не пройдено)."""
    parse_report, patch = gate_parse_ok(text, recount=True)
    base_report, new = gate_base_version(patch, files)
    reports = [parse_report, gate_allowed_paths(patch, tuple(f["path"] for f in files)), gate_secrets(patch, rules),
               gate_size_limits(text, patch), base_report]
    return reports, patch, new if all(r["ok"] for r in reports) else {}
