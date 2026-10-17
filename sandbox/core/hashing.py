"""Детерміновані хеші: sha256 тексту і канонічний JSON (однаковий об'єкт -> однаковий рядок)."""

import hashlib
import json


def sha256_hex(text: str) -> str:
    # UTF-8, hex у нижньому регістрі — так само, як у журналі запитів і в Postgres sha256()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
