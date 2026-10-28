"""Контекст кроків агента: що саме бачить модель і відбиток цього контексту.

Текст задачі йде окремим блоком <task>, файли — блоками <site_file ...>. Так модель (і фейковий
провайдер) читає задачу лише з <task>: збіги в списку шляхів чи у вмісті файлів не підміняють намір.
"""

from sandbox.agent.settings import ALLOWED_PATHS
from sandbox.core.hashing import canonical_json, sha256_hex


def _task_block(task_text: str) -> str:
    return f"<task>{task_text}</task>\n"


def _allowed_block() -> str:
    return "<allowed_paths>\n" + "".join(f"{p}\n" for p in ALLOWED_PATHS) + "</allowed_paths>\n"


def system_text(prompt_body: str, extra: str) -> str:
    """Версіонований промпт іде першим: він задає формат відповіді; роль і скіли — після нього."""
    return prompt_body if not extra else f"{prompt_body}\n\n{extra}"


def plan_messages(system: str, task_text: str) -> list[dict]:
    # планувальнику вміст файлів не потрібен: він лише обирає намір і файли зі списку
    return [{"role": "system", "content": system},
            {"role": "user", "content": _task_block(task_text) + _allowed_block()}]


def propose_messages(system: str, task_text: str, files: list[dict]) -> list[dict]:
    blocks = "".join(
        f'<site_file path="{f["path"]}" version="{f["version"]}" sha256="{f["sha256"]}">\n'
        f'{f["content"]}</site_file>\n' for f in files)
    return [{"role": "system", "content": system},
            {"role": "user", "content": _task_block(task_text) + _allowed_block() + blocks}]


def history(files: list[dict]) -> list[dict]:
    # історія застосованих версій цих файлів. У hw1 застосувань немає (apply з'явиться в hw4),
    # тому історія порожня; запит до agent_site_file_versions додасть hw3
    return []


def digest(files: list[dict], hist: list[dict]) -> tuple[str, dict]:
    """(context_sha256, base_versions): відбиток файлів (шлях, версія, sha) та історії.
    Той самий контекст -> той самий відбиток; з hw3 він стане частиною ключа кешу кроку propose."""
    ctx = {"files": [{"path": f["path"], "version": f["version"], "sha256": f["sha256"]} for f in files],
           "history": hist}
    return sha256_hex(canonical_json(ctx)), {f["path"]: f["version"] for f in files}
