"""FakeProvider агента: маркери збоїв як у mock-provider, токени як у mock, детермінований diff,
чутливість до system-промпта і намір лише з блоку <task>."""

import asyncio
import json

import pytest

from sandbox.agent.prompts import AGENT_FALLBACK_PROMPT
from sandbox.core.hashing import sha256_hex
from sandbox.llm import fake_fixtures as fx
from sandbox.llm.fake_provider import FakeProvider, tokens
from tests.agent.memory_repo import prompt_body, seed_files

PLANNER = prompt_body("agent-planner", "v1")
EDITOR_V1 = prompt_body("agent-editor", "v1")
EDITOR_V2 = prompt_body("agent-editor", "v2")
FILES = seed_files()


def user_text(task: str, paths=(), allowed=("demo-site/footer.html",), files=None) -> str:
    files = files or FILES
    blocks = "".join(f'<site_file path="{p}" version="1" sha256="{sha256_hex(files[p])}">\n{files[p]}</site_file>\n'
                     for p in paths)
    return f"<task>{task}</task>\n<allowed_paths>\n" + "".join(f"{a}\n" for a in allowed) + "</allowed_paths>\n" + blocks


def run(system: str, user: str, *, alias: str = "fake-mini", delay_ms: int = 0):
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return asyncio.run(FakeProvider(delay_ms=delay_ms).complete(messages, alias, timeout_s=5))


@pytest.fixture
def sleeps(monkeypatch):
    """Підміняє asyncio.sleep і записує, скільки секунд просили поспати."""
    calls: list[float] = []

    async def fake_sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return calls


def test_fail_503_on_every_attempt():
    for attempt in (1, 2):
        messages = [{"role": "system", "content": PLANNER},
                    {"role": "user", "content": user_text("__fail_503 Онови рік у підвалі сайту")}]
        res = asyncio.run(FakeProvider(delay_ms=0).complete(messages, "fake-mini", timeout_s=5, attempt=attempt))
        assert (res.ok, res.status, res.error, res.provider) == (False, 503, "http_503", "fake")
        assert (res.usage.prompt_tokens, res.usage.completion_tokens) == (0, 0)


@pytest.mark.parametrize("task", ["__fail_503 __fail_429 footer", "__fail_429 __fail_503 footer"])
def test_429_wins_over_503(task):
    res = run(PLANNER, user_text(task))
    assert (res.ok, res.status) == (False, 429)


def test_fail_is_checked_before_delay_and_garbage(sleeps):
    res = run(PLANNER, user_text("__garbage __delay __fail_503 Онови рік у підвалі сайту"), delay_ms=5000)
    assert res.status == 503 and sleeps == []


def test_delay_is_non_blocking_sleep_in_ms(sleeps):
    res = run(PLANNER, user_text("__delay Онови рік у підвалі сайту"), delay_ms=1500)
    assert sleeps == [1.5]
    assert res.ok and json.loads(res.content)["intent"] == "footer_year"


def test_garbage_is_dots_with_200():
    res = run(EDITOR_V2, user_text("__garbage Онови рік у підвалі сайту", ["demo-site/footer.html"]))
    assert (res.ok, res.status, res.content) == (True, 200, "...")


def test_fail_first_is_plain_text_until_hw4():
    res = run(PLANNER, user_text("__fail_first Онови рік у підвалі сайту"))
    assert res.ok and json.loads(res.content)["intent"] == "footer_year"


def test_markers_outside_task_block_are_ignored():
    files = dict(FILES)
    files["demo-site/footer.html"] = FILES["demo-site/footer.html"] + "<!-- __fail_503 __garbage -->\n"
    res = run(EDITOR_V2, user_text("Онови рік у підвалі сайту", ["demo-site/footer.html"],
                                   allowed=("__fail_429",), files=files))
    assert res.ok and res.content.startswith("--- a/demo-site/footer.html\n")


def test_tokens_like_mock():
    assert tokens("") == 0 and tokens("abc") == 1 and tokens("a" * 9) == 2
    user = user_text("Онови рік у підвалі сайту")
    res = run(PLANNER, user)
    assert res.usage.prompt_tokens == tokens(PLANNER) + tokens(user)
    assert res.usage.completion_tokens == tokens(res.content)


def test_header_diff_is_deterministic_and_matches_seed():
    user = user_text("Внеси зміни в шапку сайту: додай кнопку логіну", ["demo-site/header.html", "demo-site/styles.css"])
    first, second = run(EDITOR_V2, user), run(EDITOR_V2, user)
    assert first.content == second.content
    diff = first.content
    assert diff.startswith("--- a/demo-site/header.html\n+++ b/demo-site/header.html\n@@ -4,4 +4,5 @@\n")
    assert "--- a/demo-site/styles.css\n+++ b/demo-site/styles.css\n@@ -3,3 +3,4 @@\n" in diff
    assert "+" + fx.LOGIN_LINK in diff and "+" + fx.LOGIN_CSS in diff


@pytest.mark.parametrize("system,expected", [
    (EDITOR_V1, fx.NO_FORMAT_TEXT),
    (AGENT_FALLBACK_PROMPT, fx.NO_FORMAT_TEXT),
])
def test_without_format_in_prompt_there_is_no_plan_or_diff(system, expected):
    res = run(system, user_text("Онови рік у підвалі сайту", ["demo-site/footer.html"]))
    assert res.ok and res.content == expected


def test_prompt_sensitivity_planner_and_editor():
    plan = run(PLANNER, user_text("Онови рік у підвалі сайту"))
    assert json.loads(plan.content) == {"intent": "footer_year", "files": ["demo-site/footer.html"],
                                        "complexity": "simple"}
    edit = run(EDITOR_V2, user_text("Онови рік у підвалі сайту", ["demo-site/footer.html"]))
    assert "-  <p>© 2025 Demo Site</p>\n+  <p>© 2026 Demo Site</p>\n" in edit.content


def test_intent_comes_only_from_task_block():
    # footer.html у allowed_paths і «©» у вмісті файла не роблять задачу «про підвал»
    res = run(PLANNER, user_text("Change the brand color to green", ["demo-site/footer.html"],
                                 allowed=("demo-site/footer.html",)))
    assert json.loads(res.content)["intent"] == "color_change"
    assert json.loads(res.content)["files"] == ["demo-site/styles.css"]


@pytest.mark.parametrize("task,intent,files,complexity", [
    ("Внеси зміни в шапку сайту: додай кнопку логіну", "header_login_button",
     ["demo-site/header.html", "demo-site/styles.css"], "complex"),
    ("Внеси изменения в шапку сайта: добавь кнопку логина", "header_login_button",
     ["demo-site/header.html", "demo-site/styles.css"], "complex"),
    ("Add a login button to the site header", "header_login_button",
     ["demo-site/header.html", "demo-site/styles.css"], "complex"),
    ("Онови рік у підвалі сайту", "footer_year", ["demo-site/footer.html"], "simple"),
    ("Update the year in the site footer", "footer_year", ["demo-site/footer.html"], "simple"),
    ("Change the brand color to green", "color_change", ["demo-site/styles.css"], "simple"),
    ("Додай на сайт аналітику з зовнішнім трекером", "analytics_script", ["demo-site/index.html"], "complex"),
    ("Зроби сайт кращим", "unknown", [], "simple"),
])
def test_intent_table(task, intent, files, complexity):
    res = run(PLANNER, user_text(task))
    assert json.loads(res.content) == {"intent": intent, "files": files, "complexity": complexity}


@pytest.mark.parametrize("task,path,patched", [
    ("Внеси зміни в шапку сайту: додай кнопку логіну", "demo-site/header.html",
     lambda s: s.replace("</header>", fx.LOGIN_LINK + "</header>")),
    ("Онови рік у підвалі сайту", "demo-site/footer.html", lambda s: s.replace("2025", "2026")),
    ("Change the brand color to green", "demo-site/styles.css", lambda s: s.replace("#1d4ed8", fx.GREEN)),
])
def test_no_change_when_already_done(task, path, patched):
    files = dict(FILES)
    files[path] = patched(FILES[path])
    res = run(EDITOR_V2, user_text(task, [path], files=files))
    assert res.ok and res.content == fx.NO_CHANGE


def test_analytics_adds_external_script():
    res = run(EDITOR_V2, user_text("Додай на сайт аналітику з зовнішнім трекером", ["demo-site/index.html"]))
    assert '+  <script src="https://cdn.example.com/track.js"></script>\n' in res.content


def test_unknown_alias_is_404():
    res = run(PLANNER, user_text("Онови рік у підвалі сайту"), alias="mock")
    assert (res.ok, res.status, res.error) == (False, 404, "unknown_alias")


def _chunk(path, first, lines):
    return path, "".join(f"{first + i}| {line}\n" for i, line in enumerate(lines))


def test_explorer_cites_section_under_matching_heading():
    chunks = [_chunk("README.md", 1, ["Сервіс нотаток зберігає нотатки у сховищі SQLite.", "Нотатки мають теги."]),
              _chunk("docs/ARCHITECTURE.md", 20, ["## Інваріанти", "", "1. Ідентифікатор лише зростає.", "2. Видалення мʼяке.",
                                                  "## Конкурентність", "Один процес."])]
    out = json.loads(fx.explore_json("Які інваріанти має сховище нотаток?", chunks, "system"))
    assert out["answer"].startswith(fx.EXPLORE_LABEL)
    assert "«Інваріанти»" in out["answer"] and "Ідентифікатор лише зростає" in out["answer"] and "Один процес" not in out["answer"]
    assert out["citations"] == [{"path": "docs/ARCHITECTURE.md", "lines": "20-23"}]


def test_explorer_ignores_lines_that_echo_the_question():
    question = "Де перевіряється результат?"
    chunks = [_chunk("ui/catalog.js", 1, [f'{{ goal: "{question}" }}']),
              _chunk("pipeline.py", 10, ["Результат перевіряють гейти коду."])]
    out = json.loads(fx.explore_json(question, chunks, "system"))
    assert [c["path"] for c in out["citations"]] == ["pipeline.py"]


def test_explorer_without_matching_words_reports_missing():
    out = json.loads(fx.explore_json("Що таке квазар?", [_chunk("a.md", 1, ["Нотатки й теги."])], "system"))
    assert out["citations"] == [] and out["missing"] and out["answer"].startswith(fx.EXPLORE_LABEL)
