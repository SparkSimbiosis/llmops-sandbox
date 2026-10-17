"""Гейти пропозиції агента. hw1 — два: parse_ok (це справді diff) і allowed_paths (лише файли
зі списку). Решта (заборонені патерни, ліміти розміру, версія бази) — з hw4.

Кожен гейт повертає {"gate": ..., "ok": ...}; хоч один ok:false -> статус rejected_by_gate.
"""

import re

from sandbox.agent.patches import NULL_PATH, ParsedPatch, PatchParseError, parse_unified_diff
from sandbox.agent.settings import ALLOWED_PATHS

_DRIVE = re.compile(r"^[A-Za-z]:")


def _shown(path: str) -> str:
    # NUL не можна покласти ні в jsonb, ні в text Postgres — показуємо його екранованим
    return path.replace("\x00", "\\x00")


def path_problem(path: str) -> str | None:
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
    if path not in ALLOWED_PATHS:
        return "not_allowed"
    return None


def gate_parse_ok(text: str) -> tuple[dict, ParsedPatch | None]:
    try:
        patch = parse_unified_diff(text)
    except PatchParseError as exc:
        return {"gate": "parse_ok", "ok": False, "error": str(exc)}, None
    return {"gate": "parse_ok", "ok": True}, patch


def gate_allowed_paths(patch: ParsedPatch | None) -> dict:
    if patch is None:
        # без розбору шляхи невідомі — гейт закритий, а не «пропущений»
        return {"gate": "allowed_paths", "ok": False, "error": "not checked: diff did not parse"}
    violations = []
    for f in patch.files:
        if f.old_path != f.new_path:
            violations.append({"path": _shown(f.new_path), "reason": "rename"})
        for path in dict.fromkeys((f.old_path, f.new_path)):
            reason = path_problem(path)
            if reason:
                violations.append({"path": _shown(path), "reason": reason})
    if violations:
        return {"gate": "allowed_paths", "ok": False, "violations": violations}
    return {"gate": "allowed_paths", "ok": True}


def run_gates(text: str) -> tuple[list[dict], ParsedPatch | None]:
    parse_report, patch = gate_parse_ok(text)
    return [parse_report, gate_allowed_paths(patch)], patch
