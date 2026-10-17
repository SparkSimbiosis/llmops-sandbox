"""Розбір unified diff, який повертає модель (hw1 — лише розбір і шляхи).

Застосування diff у пам'яті з перерахунком ханків з'явиться в hw3, коли знадобиться dry-run.
Розбір строгий: заголовки ---/+++, ханки @@ і довжини ханків мають збігатися з рядками,
інакше це не diff, а текст, якому не можна довіряти.
"""

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


def parse_unified_diff(text: str) -> ParsedPatch:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()                             # завершальний перевід рядка
    files: list[FilePatch] = []
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
            # читаємо рівно стільки рядків, скільки обіцяє заголовок: рядок "--- x" усередині
            # ханка — це видалений рядок, а не заголовок наступного файла
            while i < n and (seen_old < old_len or seen_new < new_len):
                body_line = lines[i]
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
    return ParsedPatch(tuple(files))


def diff_paths(text: str) -> list[str]:
    """М'яке читання шляхів для показу збереженої задачі (навіть якщо diff не пройшов розбір)."""
    try:
        return parse_unified_diff(text).paths
    except PatchParseError:
        return [_header_path(line, "b/") for line in text.splitlines()
                if line.startswith("+++ ") and _header_path(line, "b/") != NULL_PATH]
