"""SQLite store: the only module that writes to the database (see docs/ARCHITECTURE.md)."""

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

TITLE_MAX = 120
BODY_MAX = 10_000
TAGS_MAX = 8
RETENTION_DAYS = 30
_TAG = re.compile(r"^[a-z0-9-]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,  -- AUTOINCREMENT: ids are never reused (invariant 1)
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
"""


class LimitError(ValueError):
    """Maps to HTTP 422; .code is the error code from docs/API.md."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


class RetentionExpired(Exception):
    """Restore after RETENTION_DAYS -> HTTP 410."""


@dataclass
class Note:
    id: int
    title: str
    body: str
    tags: list[str]
    created_at: str
    updated_at: str
    deleted_at: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_tags(tags: list[str] | None) -> list[str]:
    out = sorted({t.strip().lower() for t in (tags or []) if t.strip()})
    if len(out) > TAGS_MAX:
        raise LimitError("too_many_tags", f"at most {TAGS_MAX} tags")
    bad = [t for t in out if not _TAG.match(t)]
    if bad:
        raise LimitError("bad_tag", f"tags must match [a-z0-9-]: {bad[0]}")
    return out


def check_limits(title: str, body: str) -> None:
    if len(title) > TITLE_MAX:
        raise LimitError("title_too_long", f"title is longer than {TITLE_MAX}")
    if len(body) > BODY_MAX:
        raise LimitError("body_too_long", f"body is longer than {BODY_MAX}")


class Store:
    def __init__(self, path: str = "notes.db"):
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def _row(self, row) -> Note:
        return Note(row["id"], row["title"], row["body"], [t for t in row["tags"].split(",") if t],
                    row["created_at"], row["updated_at"], row["deleted_at"])

    def create(self, title: str, body: str, tags: list[str] | None = None) -> Note:
        check_limits(title, body)
        clean = normalize_tags(tags)
        now = _now().isoformat()
        self.conn.execute("BEGIN IMMEDIATE")
        cur = self.conn.execute(
            "INSERT INTO notes (title, body, tags, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (title, body, ",".join(clean), now, now))
        self.conn.execute("COMMIT")
        return self.get(cur.lastrowid)

    def get(self, note_id: int, include_deleted: bool = False) -> Note | None:
        row = self.conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        if row is None or (row["deleted_at"] and not include_deleted):
            return None
        return self._row(row)

    def update(self, note_id: int, **fields) -> Note | None:
        note = self.get(note_id)
        if note is None:
            return None
        title = fields.get("title", note.title)
        body = fields.get("body", note.body)
        tags = normalize_tags(fields["tags"]) if "tags" in fields else note.tags
        check_limits(title, body)
        if (title, body, tags) == (note.title, note.body, note.tags):
            return note                      # invariant 4: no real change -> updated_at stays
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("UPDATE notes SET title = ?, body = ?, tags = ?, updated_at = ? WHERE id = ?",
                          (title, body, ",".join(tags), _now().isoformat(), note_id))
        self.conn.execute("COMMIT")
        return self.get(note_id)

    def delete(self, note_id: int) -> bool:
        cur = self.conn.execute("UPDATE notes SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                                (_now().isoformat(), note_id))
        return cur.rowcount == 1

    def restore(self, note_id: int) -> Note | None:
        note = self.get(note_id, include_deleted=True)
        if note is None or note.deleted_at is None:
            return None
        if datetime.fromisoformat(note.deleted_at) < _now() - timedelta(days=RETENTION_DAYS):
            raise RetentionExpired(note_id)
        self.conn.execute("UPDATE notes SET deleted_at = NULL WHERE id = ?", (note_id,))
        return self.get(note_id)

    def list_active(self) -> list[Note]:
        rows = self.conn.execute("SELECT * FROM notes WHERE deleted_at IS NULL ORDER BY id").fetchall()
        return [self._row(r) for r in rows]

    def purge_deleted(self) -> int:
        cutoff = (_now() - timedelta(days=RETENTION_DAYS)).isoformat()
        cur = self.conn.execute("DELETE FROM notes WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,))
        return cur.rowcount
