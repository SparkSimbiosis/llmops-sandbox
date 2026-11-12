"""Linear search over active notes (ADR-003). Reads through Store, never touches the database itself."""

import re

from notes.store import Note, Store

LIMIT_MAX = 100
TITLE_WEIGHT = 3
BODY_WEIGHT = 1
_WORD = re.compile(r"\w{2,}", re.UNICODE)


def words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


def rank(note: Note, terms: list[str]) -> int:
    """0 means "does not match": every term must be present in the title or the body."""
    title, body = set(words(note.title)), set(words(note.body))
    score = 0
    for t in terms:
        if t in title:
            score += TITLE_WEIGHT
        elif t in body:
            score += BODY_WEIGHT
        else:
            return 0
    return score


def search(store: Store, q: str, tag: str | None = None, limit: int = 20) -> list[Note]:
    terms = words(q)
    if not terms:
        raise ValueError("empty query")
    limit = max(1, min(limit, LIMIT_MAX))
    hits = []
    for note in store.list_active():          # list_active already skips deleted notes (invariant 3)
        if tag and tag.lower() not in note.tags:
            continue
        score = rank(note, terms)
        if score:
            hits.append((score, note.updated_at, note))
    hits.sort(key=lambda h: (h[0], h[1]), reverse=True)    # equal rank -> newer first
    return [n for _, _, n in hits[:limit]]
