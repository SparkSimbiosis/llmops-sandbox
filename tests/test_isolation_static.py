"""Статична ізоляція dev-агента від курсу (ast і регулярки, без запуску сервісу).

Шаблони, які збігалися б із самим цим файлом при grep ментора, зібрано без літерала.
"""

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SANDBOX = ROOT / "sandbox"
AGENT_LLM = ("fake_provider", "fake_fixtures", "provider_router", "fault_injector", "budget_guard")

GUARDED_BLOCK = '''if os.getenv("AGENT_ENABLED", "1") == "1":
    try:
        from sandbox.api.routes_agent import router as agent_router
        app.include_router(agent_router)
    except Exception:
        log.exception("agent router disabled")
'''

AGENT_MAY_IMPORT = {"sandbox.core.config", "sandbox.core.hashing", "sandbox.core.pii",
                    "sandbox.llm.types", "sandbox.llm.gateway_provider"} | {f"sandbox.llm.{m}" for m in AGENT_LLM}


def py_files() -> list[pathlib.Path]:
    return sorted(p for p in SANDBOX.rglob("*.py") if "__pycache__" not in p.parts)


def dotted(path: pathlib.Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def is_agent_module(name: str) -> bool:
    return (name == "sandbox.agent" or name.startswith("sandbox.agent.")
            or name in {f"sandbox.llm.{m}" for m in AGENT_LLM} or name == "sandbox.api.routes_agent")


AGENT_FILES = [p for p in py_files() if is_agent_module(dotted(p))]
COURSE_FILES = [p for p in py_files() if not is_agent_module(dotted(p)) and p.name != "main.py"]
MAIN = SANDBOX / "main.py"


def _is_module(name: str) -> bool:
    base = ROOT.joinpath(*name.split("."))
    return base.with_suffix(".py").exists() or (base / "__init__.py").exists()


def sandbox_imports(tree: ast.AST) -> list[str]:
    """Імпорти з пакета sandbox, зведені до модулів (from sandbox.agent import gates -> sandbox.agent.gates)."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] == "sandbox"]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports are not used in sandbox"
            if node.module and node.module.split(".")[0] == "sandbox":
                for a in node.names:
                    sub = f"{node.module}.{a.name}"
                    found.append(sub if _is_module(sub) else node.module)
    return found


def read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def test_agent_module_set_is_what_we_expect():
    names = {dotted(p) for p in AGENT_FILES}
    assert {"sandbox.agent.pipeline", "sandbox.agent.repo", "sandbox.llm.fake_provider",
            "sandbox.llm.provider_router", "sandbox.api.routes_agent"} <= names


# 1. жодного seed-INSERT у таблиці промптів у коді сервісу (курсових і агентських)
def test_no_prompt_seed_insert_in_sandbox():
    pat = re.compile(r"insert\s+into\s+(agent_)?" + "prompts", re.I)
    hits = [str(p) for p in SANDBOX.rglob("*") if p.is_file() and "__pycache__" not in p.parts
            and pat.search(p.read_text(encoding="utf-8", errors="replace"))]
    assert hits == []


# 2. агентський код не читає й не пише курсові таблиці
def test_agent_modules_do_not_touch_course_tables():
    pat = re.compile(r"(?i)\b(from|into|update|join)\s+(requests|prompts|prompt_activations)\b")
    hits = [(p.name, m.group(0)) for p in AGENT_FILES for m in pat.finditer(read(p))]
    assert hits == []


# 3. allowlist імпортів в обидва боки
def test_agent_imports_only_allowlisted_sandbox_modules():
    bad = []
    for p in AGENT_FILES:
        for name in sandbox_imports(ast.parse(read(p))):
            if not (name in AGENT_MAY_IMPORT or is_agent_module(name)):
                bad.append((p.name, name))
    assert bad == []


def test_course_modules_do_not_import_agent():
    bad = [(p.name, n) for p in COURSE_FILES for n in sandbox_imports(ast.parse(read(p))) if is_agent_module(n)]
    assert bad == []


def test_main_has_exactly_one_guarded_agent_block():
    text = read(MAIN)
    assert text.count(GUARDED_BLOCK) == 1
    rest = text.replace(GUARDED_BLOCK, "")
    assert [n for n in sandbox_imports(ast.parse(rest)) if is_agent_module(n)] == []
    assert "routes_agent" not in rest and "sandbox.agent" not in rest


# 4. схема: курсовий seed недоторканий, агентський блок після курсового, у маркерах, лише agent_*
def test_schema_course_seed_and_agent_block():
    text = read(ROOT / "db" / "schema.sql")
    course_insert = "INSERT INTO " + "prompts"
    assert text.count(course_insert) == 1
    stmt = text[text.index(course_insert):]
    stmt = stmt[:stmt.index("ON CONFLICT")]
    assert re.findall(r"\(\s*'([^']*)'\s*,\s*'(v\d+)'", stmt) == [("support-system", "v1"), ("support-system", "v2")]

    begin, end = "-- === hw1: agent (початок) ===", "-- === hw1: agent (кінець) ==="
    assert text.count(begin) == 1 and text.count(end) == 1
    b, e = text.index(begin), text.index(end)
    assert text.index(course_insert) < b < e
    assert "agent_" not in text[:b]                      # до маркера — лише курс
    tail = text[e + len(end):].strip()
    assert tail == "" or tail.startswith("-- === hw")    # далі можуть іти лише блоки наступних тижнів

    block = text[b:e + len(end)]
    created = re.findall(r"CREATE\s+(?:UNIQUE\s+)?(?:OR\s+REPLACE\s+)?(TABLE|VIEW|INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
                         block, re.I)
    tables = {n for kind, n in created if kind.upper() == "TABLE"}
    views = {n for kind, n in created if kind.upper() == "VIEW"}
    indexes = [n for kind, n in created if kind.upper() == "INDEX"]
    assert tables == {"agent_prompts", "agent_prompt_activations", "agent_site_file_versions",
                      "agent_tasks", "agent_calls"}
    assert views == {"agent_site_files_current"}
    assert indexes and all("agent_" in n for n in indexes)
    # ключові слова SQL у блоці — великими літерами; так «from the» у тексті промпта не рахується
    for pattern in (r"INSERT\s+INTO\s+(\w+)", r"REFERENCES\s+(\w+)", r"\bON\s+(?!CONFLICT\b)(\w+)\s*\(",
                    r"\bFROM\s+([A-Za-z_]\w*)"):
        targets = re.findall(pattern, block)
        assert all(t.startswith("agent_") for t in targets), (pattern, targets)

    # схема росте по тижнях: колонок і таблиць hw2–hw4 у блоці hw1 немає. Рахуємо лише код SQL:
    # спершу прибираємо $f$-вміст файлів (у ньому є «<!--»), потім коментарі, потім рядкові літерали
    code = re.sub(r"\$f\$.*?\$f\$", "", block, flags=re.S)
    code = re.sub(r"--[^\n]*", "", code)
    code = re.sub(r"'(?:[^']|'')*'", "''", code)
    later = ("complexity", "total_cost_usd", "cost_usd", "cache_hit", "attempt", "fault_injected",
             "source_task_id", "approval_id", "agent_approvals")
    assert [w for w in later if re.search(rf"\b{w}\b", code)] == []

    # з hw2: кожна міграція агента входить у schema.sql дослівно, після блоку hw1, у порядку тижнів
    pos = e
    for mig in sorted((ROOT / "db" / "agent_migrations").glob("hw*_*.sql")):
        body = read(mig)
        assert body in text, mig.name
        assert text.index(body) > pos, mig.name
        pos = text.index(body)


# 5. жодного запису на диск, subprocess, shutil; в агенті — жодного блокуючого sleep
def _write_open_calls(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if name in ("write_text", "write_bytes"):
                lines.append(node.lineno)
            if name == "open":
                mode = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "mode"), None)
                if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and set(mode.value) & set("wax+"):
                    lines.append(node.lineno)
    return lines


def test_no_disk_writes_or_processes_in_sandbox():
    pat = re.compile(r"\bsubprocess\b|\bos\.system\b|\bshutil\b|\bos\.popen\b")
    bad = [(p.name, m.group(0)) for p in py_files() for m in pat.finditer(read(p))]
    bad += [(p.name, f"open/write at line {n}") for p in py_files() for n in _write_open_calls(ast.parse(read(p)))]
    assert bad == []


def test_agent_has_no_blocking_sleep():
    bad = [p.name for p in AGENT_FILES if re.search(r"\btime\.sleep\b", read(p))]
    for p in AGENT_FILES:
        for node in ast.walk(ast.parse(read(p))):
            if isinstance(node, ast.ImportFrom) and node.module == "time" and any(a.name == "sleep" for a in node.names):
                bad.append(p.name)
    assert bad == []


# 6. REAL ніколи не вмикається в CI; імен ключів у коді сервісу немає
def test_workflows_never_set_agent_provider_mode():
    hits = [p.name for p in (ROOT / ".github" / "workflows").glob("*.yml") if "AGENT_PROVIDER_MODE" in read(p)]
    assert hits == []


def test_no_key_names_in_sandbox():
    needle = "API" + "_KEY"
    assert [p.name for p in py_files() if needle in read(p)] == []


# 7. рядки логу агента не схожі на курсові: grep ментора по `chat request_id=` і `cache=` їх не зачепить
def test_agent_log_strings_do_not_look_like_course():
    pat = re.compile(r"(?<!agent_)cache=(hit|miss)|chat request_id=")
    assert [(p.name, m.group(0)) for p in AGENT_FILES for m in pat.finditer(read(p))] == []
