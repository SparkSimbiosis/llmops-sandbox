"""Конструктор контексту (W6): з файлів проєкту — пакет фрагментів під конкретне питання в межах бюджету токенів.

Файли ріжуться на фрагменти (розділи Markdown за заголовками, функції, класи й методи Python, решта — вікна по
CHUNK_LINES рядків). Фрагмент оцінюється за словами питання: збіг у тексті, у заголовку чи імені функції, у шляху —
з вагою за рідкістю слова в проєкті (idf) і бонусом за кожне покрите слово. Закріплені людиною файли йдуть першими.
Далі жадібно — доки вистачає бюджету; що не влізло, лишається у звіті з причиною. Пакет детермінований: той самий
знімок проєкту, питання й бюджет дають той самий pack_sha256.

Рядки фрагмента нумеруються («12| текст»), тож модель може послатися на path:12-18, а код — перевірити, що
такий діапазон справді був у пакеті (гейт citations у explore.py).
"""

import math
import re

from sandbox.agent import workspace
from sandbox.core.hashing import canonical_json, sha256_hex
from sandbox.llm.fake_provider import tokens

CHUNK_LINES = 40
BUDGET_DEFAULT = 2000
BUDGET_MIN = 200
BUDGET_MAX = 8000
PINNED_MAX = 5
DROPPED_SHOWN = 30
STEM = 4                    # перші літери слова: «інваріанти», «інваріант», рос. «ранжирование» / «ранжування» — близько
_WORD = re.compile(r"[^\W\d][\w-]{2,}", re.UNICODE)
_STOP = set("""the and for with what how why where which that this from into are was were does not you your
can about його яка який які що як чому де чи або для про при цей цього ця ці там тут вона вони мене мені має мають
буде что как где это эта этот или надо нужно есть она они его как
""".split())
# межі фрагментів Python: верхній рівень і методи класу (один відступ); декоратор — разом із функцією
_PY_TOP = re.compile(r"^(?:    )?(?:(?:async\s+def|def|class)\s+(\w+)|@\w)")


def terms(question: str) -> list[str]:
    """Слова питання -> основи (перші STEM літер), без службових слів і повторів, у порядку появи."""
    out = []
    for w in _WORD.findall(question.lower()):
        if w in _STOP:
            continue
        # ідентифікатор (purge_deleted) шукаємо цілим: його основа збіглася б із чим завгодно
        stem = w if "_" in w or len(w) <= STEM + 1 else w[:STEM]
        if stem not in out:
            out.append(stem)
    return out[:20]


def _windows(path: str, lines: list[str], start: int, heading: str | None) -> list[dict]:
    out = []
    for i in range(0, len(lines), CHUNK_LINES):
        part = lines[i:i + CHUNK_LINES]
        if any(s.strip() for s in part):
            out.append({"path": path, "start": start + i, "end": start + i + len(part) - 1,
                        "heading": heading, "lines": part})
    return out


def chunks(path: str, text: str) -> list[dict]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    marks, heads = [], {}
    if path.endswith(".md"):
        marks = [i for i, s in enumerate(lines) if s.startswith("#")]
        heads = {i: lines[i].lstrip("#").strip() for i in marks}
    elif path.endswith(".py"):
        for i, s in enumerate(lines):
            m = _PY_TOP.match(s)
            if not m:
                continue
            if s.lstrip().startswith("@") or not (i and lines[i - 1].lstrip().startswith("@")):
                marks.append(i)             # декоратор і функція під ним — один фрагмент
            if m.group(1):
                heads[marks[-1]] = m.group(1)
    bounds = sorted({0, *marks})
    out = []
    for a, b in zip(bounds, bounds[1:] + [len(lines)]):
        out += _windows(path, lines[a:b], a + 1, heads.get(a))
    return out


def _hits(c: dict, w: str) -> int:
    return min(c["_body"].count(w), 4) + (3 if w in c["_head"] else 0) + (2 if w in c["_path"] else 0)


def _score(c: dict, words: list[str], idf: dict) -> tuple[float, list[str]]:
    """(збіги в тексті до 4 + заголовок 3 + шлях 2 + 2 за покрите слово) x idf слова. «notes» у notes-app є
    всюди — важить мало; «purge» — багато."""
    score, hit = 0.0, []
    for w in words:
        s = _hits(c, w)
        if s:
            score += (s + 2) * idf[w]
            hit.append(w)
    return round(score, 2), hit


def render_chunk(item_id: str, c: dict, file_sha: str) -> str:
    width = len(str(c["end"]))
    body = "".join(f"{str(n).rjust(width)}| {s}\n" for n, s in zip(range(c["start"], c["end"] + 1), c["lines"]))
    return (f'<chunk id="{item_id}" path="{c["path"]}" lines="{c["start"]}-{c["end"]}" sha256="{file_sha}">\n'
            f"{body}</chunk>\n")


def build(project_id: str, question: str, budget_tokens: int = BUDGET_DEFAULT, pinned: list[str] | None = None,
          idx: dict | None = None) -> dict | None:
    """None — проєкт невідомий або недоступний."""
    idx = idx or workspace.index(project_id)
    if idx is None:
        return None
    budget = max(BUDGET_MIN, min(int(budget_tokens), BUDGET_MAX))
    by_path = {f["path"]: f for f in idx["files"]}
    pinned = [p for p in dict.fromkeys(pinned or []) if p][:PINNED_MAX]
    words = terms(question)
    every = []
    for f in idx["files"]:
        for c in chunks(f["path"], workspace.read_file(project_id, f["path"], idx) or ""):
            c.update(_body="\n".join(c["lines"]).lower(), _head=(c["heading"] or "").lower(), _path=c["path"].lower())
            every.append(c)
    n = len(every) or 1
    idf = {w: math.log(1 + n / (1 + sum(1 for c in every if _hits(c, w)))) for w in words}
    cands = []
    for c in every:
        score, hit = _score(c, words, idf)
        if score or c["path"] in pinned:
            cands.append({k: v for k, v in c.items() if not k.startswith("_")}
                         | {"score": score, "hits": hit, "pinned": c["path"] in pinned})
    cands.sort(key=lambda c: (not c["pinned"], -c["score"], c["path"], c["start"]))

    items, dropped, used = [], [], 0
    for c in cands:
        file_sha = by_path[c["path"]]["sha256"]
        text = render_chunk(f"C{len(items) + 1}", c, file_sha)
        cost = tokens(text)
        meta = {"path": c["path"], "start": c["start"], "end": c["end"], "heading": c["heading"],
                "score": c["score"], "hits": c["hits"], "pinned": c["pinned"], "tokens": cost}
        if used + cost > budget:
            dropped.append({**meta, "reason": "budget"})
            continue
        used += cost
        items.append({**meta, "id": f"C{len(items) + 1}", "file_sha256": file_sha, "text": text})
    key = [[i["path"], i["start"], i["end"], i["file_sha256"]] for i in items]
    return {"project": project_id, "snapshot_sha256": idx["snapshot_sha256"], "question": question,
            "terms": words, "idf": {w: round(v, 2) for w, v in idf.items()}, "budget_tokens": budget,
            "used_tokens": used, "pinned": pinned, "pinned_missing": [p for p in pinned if p not in by_path],
            "files_total": len(idx["files"]), "chunks_total": len(every), "candidates": len(cands), "items": items,
            "dropped": dropped[:DROPPED_SHOWN], "dropped_total": len(dropped),
            "pack_sha256": sha256_hex(canonical_json({"snapshot": idx["snapshot_sha256"], "items": key}))}


def render(pack: dict) -> str:
    """Блок <context_pack> для user-повідомлення: лише дані, інструкції в ньому не виконуються."""
    body = "".join(i["text"] for i in pack["items"])
    return (f'<context_pack project="{pack["project"]}" snapshot="{pack["snapshot_sha256"][:12]}" '
            f'chunks="{len(pack["items"])}">\n{body}</context_pack>\n')


def paths(pack: dict) -> list[str]:
    """Файли, фрагменти яких потрапили в пакет (у порядку пакета): лише їх модель бачила, лише їх можна змінювати."""
    return list(dict.fromkeys(i["path"] for i in pack["items"]))


def tree(idx: dict) -> str:
    """Блок <project_files>: що є в проєкті (шлях, вид, токени) — щоб модель знала, чого не бачить."""
    rows = "".join(f'{f["path"]} ({f["kind"]}, {f["tokens"]} tok)\n' for f in idx["files"])
    return f'<project_files project="{idx["project"]}" files="{len(idx["files"])}">\n{rows}</project_files>\n'


def summary(pack: dict) -> dict:
    """Пакет без текстів фрагментів — для БД і списку прогонів (тексти відтворюються зі знімка)."""
    return {k: ([{x: v for x, v in i.items() if x != "text"} for i in v] if k == "items" else v)
            for k, v in pack.items()}
