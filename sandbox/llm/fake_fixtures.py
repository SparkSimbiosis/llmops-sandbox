"""Детерміновані «відповіді моделі» для FakeProvider агента: намір задачі і правки файлів demo-site.

Таблиця намірів перевіряється згори вниз — перший збіг перемагає. Маркери шукаються лише
в тексті задачі (блок <task>), у нижньому регістрі.
"""

import difflib
import json
import re

FAKE_YEAR = 2026
GREEN = "#16a34a"

HEADER = "demo-site/header.html"
FOOTER = "demo-site/footer.html"
STYLES = "demo-site/styles.css"
INDEX = "demo-site/index.html"

LOGIN_LINK = '  <a class="btn-login" href="/login">Увійти</a>\n'
LOGIN_CSS = ".btn-login { border: 1px solid #fff; border-radius: 6px; padding: 4px 12px; }\n"
# навмисний порушник для демо гейтів: зовнішній скрипт (гейт forbidden_patterns ловитиме його з hw4)
TRACKER = '  <script src="https://cdn.example.com/track.js"></script>\n'

NO_CHANGE = "NO_CHANGE"
# відповідь, коли system-промпт не задає формату (зламаний або відкочений промпт) — аналог «не знаю» у mock
NO_FORMAT_TEXT = "Можу допомогти із сайтом, але формат відповіді не задано."

# (intent, групи маркерів: потрібен збіг у кожній групі, файли, complexity)
INTENTS = (
    ("header_login_button",
     (("шапк", "хедер", "header"), ("логін", "логин", "login", "вхід", "вход", "увійти", "войти", "sign in")),
     (HEADER, STYLES), "complex"),
    ("footer_year",
     (("підвал", "подвал", "футер", "footer"), ("рік", "год", "year", "©")),
     (FOOTER,), "simple"),
    ("color_change",
     (("колір", "кольор", "цвет", "color", "colour"),),
     (STYLES,), "simple"),
    ("analytics_script",
     (("аналітик", "аналитик", "analytics", "трекер", "tracker"),),
     (INDEX,), "complex"),
)


def classify(task_text: str) -> tuple[str, tuple[str, ...], str]:
    t = task_text.lower()
    for intent, groups, files, complexity in INTENTS:
        if all(any(marker in t for marker in group) for group in groups):
            return intent, files, complexity
    return "unknown", (), "simple"


def plan_json(task_text: str) -> str:
    intent, files, complexity = classify(task_text)
    return json.dumps({"intent": intent, "files": list(files), "complexity": complexity}, ensure_ascii=False)


def _insert_before(content: str, closing: str, line: str) -> str:
    lines = content.splitlines(keepends=True)
    for i, existing in enumerate(lines):
        if existing.strip() == closing:
            return "".join(lines[:i] + [line] + lines[i:])
    return content


def _transform(intent: str, path: str, content: str) -> str:
    """Новий вміст файла для наміру; той самий вміст — змінювати нічого."""
    if intent == "header_login_button":
        if path == HEADER:
            return _insert_before(content, "</header>", LOGIN_LINK)
        if path == STYLES:
            return content + LOGIN_CSS
    if intent == "footer_year" and path == FOOTER:
        return re.sub(r"© \d{4}", f"© {FAKE_YEAR}", content)
    if intent == "color_change" and path == STYLES:
        return re.sub(r"--brand: #[0-9A-Fa-f]{3,8}", f"--brand: {GREEN}", content)
    if intent == "analytics_script" and path == INDEX:
        return content if TRACKER in content else _insert_before(content, "</body>", TRACKER)
    return content


def edit_diff(task_text: str, files: list[tuple[str, str]]) -> str:
    """Unified diff по наданих файлах (шлях, вміст) або рівно NO_CHANGE."""
    intent, _, _ = classify(task_text)
    if intent == "header_login_button" and any("btn-login" in c for p, c in files if p == HEADER):
        return NO_CHANGE   # кнопка вже є — повторна правка дала б дубль
    chunks = []
    for path, old in files:
        new = _transform(intent, path, old)
        if new != old:
            chunks.append("".join(difflib.unified_diff(
                old.splitlines(keepends=True), new.splitlines(keepends=True),
                fromfile="a/" + path, tofile="b/" + path, n=3)))
    return "".join(chunks) if chunks else NO_CHANGE
