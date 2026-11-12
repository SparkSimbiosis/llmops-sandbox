"""Розбір unified diff, який повертає модель, і його застосування до файлу в пам'яті (W4: dry-run і approve).

Розбір строгий: заголовки ---/+++, ханки @@ і довжини ханків мають збігатися з рядками,
інакше це не diff, а текст, якому не можна довіряти. Для .patch git-проєкту є режим recount (як
git apply --recount): довжини ханків рахуються з тіла, бо моделі часто помиляються в лічильниках; вміст
рядків і місце ханка все одно перевіряє apply_file. Канонічний diff (unified_diff) збирається з текстів
«до» і «після» — саме його людина завантажує як .patch.
"""

import difflib
import re
from dataclasses import dataclass

# difflib опускає довжину, коли вона 1: "@@ -3 +3,2 @@"
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
NULL_PATH = "/dev/null"


class PatchParseError(ValueError):
    pass


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: tuple[str, ...]


@dataclass(frozen=True)
class FilePatch:
    old_path: str
    new_path: str
    hunks: tuple[Hunk, ...]

    @property
    def added(self) -> int:
        return sum(1 for h in self.hunks for line in h.lines if line.startswith("+"))

    @property
    def removed(self) -> int:
        return sum(1 for h in self.hunks for line in h.lines if line.startswith("-"))


@dataclass(frozen=True)
class ParsedPatch:
    files: tuple[FilePatch, ...]
    recounted: bool = False                     # режим recount виправив лічильники хоч одного ханка

    @property
    def paths(self) -> list[str]:
        return [f.new_path if f.new_path != NULL_PATH else f.old_path for f in self.files]

    @property
    def added(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def removed(self) -> int:
        return sum(f.removed for f in self.files)


def looks_like_diff(text: str) -> bool:
    """Чи є у відповіді хоч щось схоже на diff. Ні — це статус no_patch (зламаний промпт editor),
    а не провал гейта: модель просто не відповіла у форматі."""
    return any(line.startswith(("--- ", "+++ ", "@@ ")) for line in text.splitlines())


def _header_path(line: str, prefix: str) -> str:
    path = line[4:].split("\t", 1)[0]          # після табуляції може йти дата
    if path != NULL_PATH and path.startswith(prefix):
        path = path[len(prefix):]
    return path


def _file_header(lines: list[str], i: int) -> bool:
    return lines[i].startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ ")


def parse_unified_diff(text: str, recount: bool = False) -> ParsedPatch:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()                             # завершальний перевід рядка
    if recount:
        while lines and not lines[-1].strip():
            lines.pop()                         # хвіст із порожніх рядків — не контекст ханка
    files: list[FilePatch] = []
    recounted = False
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.startswith("--- "):
            raise PatchParseError(f"line {i + 1}: expected '--- ' file header")
        if i + 1 >= n or not lines[i + 1].startswith("+++ "):
            raise PatchParseError(f"line {i + 2}: expected '+++ ' file header")
        old_path, new_path = _header_path(line, "a/"), _header_path(lines[i + 1], "b/")
        i += 2
        hunks: list[Hunk] = []
        while i < n and lines[i].startswith("@@"):
            m = _HUNK.match(lines[i])
            if m is None:
                raise PatchParseError(f"line {i + 1}: bad hunk header")
            old_start, new_start = int(m.group(1)), int(m.group(3))
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            i += 1
            body: list[str] = []
            seen_old = seen_new = 0
            # строго — рівно стільки рядків, скільки обіцяє заголовок: рядок "--- x" усередині ханка — це
            # видалений рядок, а не заголовок наступного файла; recount — до наступного @@ або пари ---/+++
            while i < n and ((seen_old < old_len or seen_new < new_len) if not recount
                             else not (lines[i].startswith("@@") or _file_header(lines, i))):
                # порожній рядок контексту без пробілу на початку — частий збій моделі
                body_line = lines[i] or (" " if recount else "")
                if body_line.startswith(" "):
                    seen_old += 1
                    seen_new += 1
                elif body_line.startswith("-"):
                    seen_old += 1
                elif body_line.startswith("+"):
                    seen_new += 1
                elif not body_line.startswith("\\"):   # "\ No newline at end of file"
                    raise PatchParseError(f"line {i + 1}: unexpected line inside hunk")
                body.append(body_line)
                i += 1
            if recount and (seen_old, seen_new) != (old_len, new_len):
                recounted, old_len, new_len = True, seen_old, seen_new
            if (seen_old, seen_new) != (old_len, new_len):
                raise PatchParseError("hunk is shorter than its header says")
            while i < n and lines[i].startswith("\\"):
                body.append(lines[i])
                i += 1
            hunks.append(Hunk(old_start, old_len, new_start, new_len, tuple(body)))
        if not hunks:
            raise PatchParseError(f"file {new_path!r}: header without hunks")
        files.append(FilePatch(old_path, new_path, tuple(hunks)))
    if not files:
        raise PatchParseError("empty diff")
    return ParsedPatch(tuple(files), recounted)


def unified_diff(path: str, old: str, new: str) -> str:
    """Канонічний diff одного файлу: a/ і b/, 3 рядки контексту, «\\ No newline» де треба — його приймає git apply.
    Порожній рядок — змін немає."""
    out = []
    for line in difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                     f"a/{path}", f"b/{path}", n=3):
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def diff_paths(text: str) -> list[str]:
    """М'яке читання шляхів для показу збереженої задачі (навіть якщо diff не пройшов розбір)."""
    try:
        return parse_unified_diff(text).paths
    except PatchParseError:
        return [_header_path(line, "b/") for line in text.splitlines()
                if line.startswith("+++ ") and _header_path(line, "b/") != NULL_PATH]


class PatchApplyError(ValueError):
    pass


def apply_file(content: str, fp: FilePatch) -> str:
    """Застосувати diff одного файлу (W4). Строго: контекст і видалені рядки мають збігатися з файлом
    рядок у рядок на місці, яке обіцяє заголовок ханка; без «fuzz» — інакше це diff до іншої версії."""
    lines = content.split("\n")
    trailing = lines and lines[-1] == ""
    if trailing:
        lines.pop()
    out: list[str] = []
    pos = 0                                       # скільки рядків оригіналу вже перенесено
    for h in fp.hunks:
        start = h.old_start - 1 if h.old_len else h.old_start
        if start < pos or start > len(lines):
            raise PatchApplyError(f"{fp.new_path}: hunk @@ -{h.old_start} is out of order or past the end")
        out += lines[pos:start]
        pos = start
        for line in h.lines:
            tag, text = line[:1], line[1:]
            if tag == "\\":
                continue
            if tag in (" ", "-"):
                if pos >= len(lines) or lines[pos] != text:
                    raise PatchApplyError(f"{fp.new_path}: line {pos + 1} does not match the diff")
                pos += 1
                if tag == " ":
                    out.append(text)
            else:
                out.append(text)
    out += lines[pos:]
    return "\n".join(out) + ("\n" if trailing else "")
