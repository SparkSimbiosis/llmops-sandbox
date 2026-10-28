"""Детерміновані «відповіді моделі» для FakeProvider агента: намір задачі і правки файлів demo-site.

Таблиця намірів перевіряється згори вниз — перший збіг перемагає. Маркери шукаються лише
в тексті задачі (блок <task>), у нижньому регістрі.
"""

import difflib
import json
import math
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


# ---------- W5: чат ----------
# Фейкова модель чату навмисно «наївна»: виконує вказівки з файлів сайту й показує свій system на прохання.
# Так пак ін'єкцій на mock показує головне: м'який шар (модель) може піддатися — жорсткий шар (гейти,
# заявка й рішення людини) однаково тримає. Справжню стійкість моделі перевіряє прогін паку на real.
CHAT_CHANGE = ("додай", "додати", "зміни", "змінити", "онови", "оновити", "застосуй", "поміняй", "зроби",
               "прибери", "добавь", "измени", "add ", "change", "update", "replace")
CHAT_LEAK = ("системн", "system prompt", "інструкці", "инструкци", "промпт")
CHAT_SECRET = ("ключ", "api key", "api_key", ".env", "токен", "token", "пароль", "password")
# вказівка «для агента» всередині файлу сайту: до кінця коментаря або рядка
AGENT_NOTE = re.compile(r"(?:для ai-агента|для агента|ai assistant|agent)\s*:\s*(.+?)\s*(?:-->|\*/|\n|$)", re.I)
CHAT_TOPICS = ((("підвал", "футер", "footer", "подвал"), FOOTER), (("шапк", "хедер", "header"), HEADER),
               (("колір", "кольор", "цвет", "color", "стил", "css"), STYLES))


def _chat(reply: str, proposal: str | None = None) -> str:
    return json.dumps({"reply": reply, "proposal": proposal}, ensure_ascii=False)


def _plain(html: str) -> str:
    return " ".join(re.sub(r"<!--.*?-->|<[^>]+>", " ", html, flags=re.S).split())


def chat_json(task_text: str, files: list[tuple[str, str]], system: str) -> str:
    t = task_text.lower()
    note = next((m.group(1) for _, content in files for m in [AGENT_NOTE.search(content)] if m), None)
    if note is not None:
        low = note.lower()
        if any(w in low for w in CHAT_LEAK):
            return _chat(system)
        if any(w in low for w in CHAT_CHANGE):
            return _chat("Виконую вказівку з файлу сайту.", note)
    if any(w in t for w in CHAT_LEAK):
        return _chat(system)
    if any(w in t for w in CHAT_CHANGE):
        return _chat("Підготував пропозицію для виконавця; застосує її людина після перегляду.", task_text)
    if any(w in t for w in CHAT_SECRET):
        return _chat("У моєму контексті немає ключів і паролів: я бачу лише файли демо-сайту.")
    path = next((p for words, p in CHAT_TOPICS if any(w in t for w in words)), INDEX)
    content = dict(files).get(path, "")
    return _chat(f"{path}: {_plain(content)[:300]}" if content else "Не бачу такого файлу серед файлів сайту.")


# ---------- W6: дослідник проєкту (explore json) ----------
# Навмисно проста модель: обирає рядки фрагментів зі словами питання (рідкісні слова важать більше, заголовки —
# ще більше) і посилається на них. Якщо найкращий рядок — заголовок, цитує розділ під ним. Відповідь явно
# позначена як Mock: це лексичний відбір, а не висновок LLM. Як і чат, наївна до інʼєкції у файлі: нотатку
# «для агента» про системний промпт виконує — тримати має гейт reply_leak.
# Маркер __fake_cite у питанні — посилання на файл, якого в пакеті не було (демо гейта citations).

EXPLORE_INJECTION = re.compile(r"agent note:.*system prompt", re.I)
EXPLORE_LABEL = "[Mock · лексичний відбір рядків, не LLM] "
_LINE = re.compile(r"^\s*(\d+)\| ?(.*)$")
_TERM = re.compile(r"[^\W\d][\w-]{2,}", re.UNICODE)
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*$")
_SECTION_LINES = 6
_QUOTE_CHARS = 220


def _stems(text: str) -> set[str]:
    return {w if len(w) <= 5 else w[:4] for w in _TERM.findall(text.lower())}


def explore_json(task_text: str, chunks: list[tuple[str, str]], system: str) -> str:
    """chunks — [(path, body з нумерованими рядками)] у порядку пакета."""
    def out(answer, citations, missing=()):
        return json.dumps({"answer": answer, "citations": citations, "missing": list(missing)}, ensure_ascii=False)

    if "__fake_cite" in task_text:
        return out("Відповідь у файлі src/notes/hidden.py.", [{"path": "src/notes/hidden.py", "lines": "1-5"}])
    for path, body in chunks:
        if EXPLORE_INJECTION.search(body):
            lines = [m.group(1) for m in map(_LINE.match, body.splitlines()) if m]
            return out("Виконую нотатку з файлу. " + system[:800], [{"path": path, "lines": f"{lines[0]}-{lines[-1]}"}])
    words = _stems(task_text)
    parsed = [(path, [(int(m.group(1)), m.group(2).strip()) for m in map(_LINE.match, body.splitlines())
                      if m and m.group(2).strip()]) for path, body in chunks]
    # Фрагменти пакета можуть перекриватися: частоту слова рахуємо по унікальних рядках.
    unique = {(path, n): text for path, rows in parsed for n, text in rows}
    df: dict[str, int] = {}
    for text in unique.values():
        for w in words & _stems(text):
            df[w] = df.get(w, 0) + 1
    echo = task_text.strip().lower()
    scored = []
    for (path, n), text in unique.items():
        shared = words & _stems(text)
        # Рядок, що містить саме питання (каталог прикладів, тест), — луна запиту, а не відповідь.
        if shared and echo not in text.lower():
            score = sum(math.log(1 + len(unique) / df[w]) for w in shared) * (2 if _HEADING.match(text) else 1)
            scored.append((score, path, n, text))
    if not scored:
        return out(EXPLORE_LABEL + "У переданих фрагментах немає рядків зі словами питання.", [], ["слова питання не знайдено в пакеті"])
    scored.sort(key=lambda s: (-s[0], s[1], s[2]))
    _score, path, n, text = scored[0]
    heading = _HEADING.match(text)
    if heading:
        # Розділ береться з того самого фрагмента, тож посилання не виходить за межі пакета.
        rows = next(rows for p, rows in parsed if p == path and any(k == n for k, _ in rows))
        section = []
        for k, line in rows:
            if k <= n:
                continue
            if _HEADING.match(line) or len(section) == _SECTION_LINES:
                break
            if not line.startswith("```"):
                section.append((k, line))
        if section:
            end = section[-1][0]
            body = " ".join(line for _, line in section)
            return out(f"{EXPLORE_LABEL}Найближчий розділ — «{heading.group(1)}» ({path}:{n}-{end}): {body}",
                       [{"path": path, "lines": f"{n}-{end}"}])
    picked = scored[:2]
    answer = " ".join(f"{p}:{k}: {t[:_QUOTE_CHARS]}" for _, p, k, t in picked)
    return out(EXPLORE_LABEL + "Рядки з найбільшою вагою слів питання — " + answer,
               [{"path": p, "lines": f"{k}-{k}"} for _, p, k, _t in picked])


# ---------- .patch для git-проєкту (repo_editor) ----------
# Mock не розуміє задачі: додає під останнім непорожнім рядком першого придатного фрагмента нотатку з її текстом.
# Цього досить, щоб пройти весь шлях: номери рядків із пакета -> гейти -> заявка -> канонічний .patch -> git apply.
REPO_NOTE = "Mock-нотатка: "
_REPO_COMMENT = (".py", ".yaml", ".yml", ".toml", ".sh", ".ps1")
_REPO_CONTEXT = 3


def repo_diff(task_text: str, chunks: list[tuple[str, str]]) -> str:
    """chunks — [(path, body з нумерованими рядками)] у порядку пакета; diff з реальними номерами рядків або NO_CHANGE."""
    note = REPO_NOTE + " ".join(task_text.split())[:160]
    for path, body in chunks:
        if path.endswith(".md"):
            added = ["", "> " + note]
        elif path.endswith(_REPO_COMMENT):
            added = ["# " + note]
        else:
            continue
        rows = [(int(m.group(1)), m.group(2)) for m in map(_LINE.match, body.splitlines()) if m]
        filled = [i for i, (_n, text) in enumerate(rows) if text.strip()]
        if not filled:
            continue
        last = filled[-1]
        context = rows[max(0, last - _REPO_CONTEXT + 1):last + 1]
        start = context[0][0]
        return (f"--- a/{path}\n+++ b/{path}\n@@ -{start},{len(context)} +{start},{len(context) + len(added)} @@\n"
                + "".join(f" {text}\n" for _n, text in context) + "".join(f"+{line}\n" for line in added))
    return NO_CHANGE
