"""Thin HTTP layer on http.server: parse the request, call store or search, return JSON (docs/API.md)."""

import argparse
import json
import os
import re
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from notes.search import search
from notes.store import LimitError, RetentionExpired, Store

_NOTE = re.compile(r"^/notes/(\d+)$")
_RESTORE = re.compile(r"^/notes/(\d+)/restore$")


def make_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload=None):
            body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self) -> dict:
            size = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(size) or b"{}")

        def _error(self, code: int, err: str, detail: str = ""):
            self._send(code, {"error": err, "detail": detail})

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/search":
                qs = parse_qs(url.query)
                try:
                    found = search(store, qs.get("q", [""])[0], qs.get("tag", [None])[0],
                                   int(qs.get("limit", ["20"])[0]))
                except ValueError as exc:
                    return self._error(422, "bad_query", str(exc))
                return self._send(200, [asdict(n) for n in found])
            m = _NOTE.match(url.path)
            note = store.get(int(m.group(1))) if m else None
            return self._send(200, asdict(note)) if note else self._error(404, "not_found")

        def do_POST(self):
            m = _RESTORE.match(self.path)
            if m:
                try:
                    note = store.restore(int(m.group(1)))
                except RetentionExpired:
                    return self._error(410, "retention_expired")
                return self._send(200, asdict(note)) if note else self._error(404, "not_found")
            if self.path != "/notes":
                return self._error(404, "not_found")
            data = self._json()
            try:
                note = store.create(data.get("title", ""), data.get("body", ""), data.get("tags"))
            except LimitError as exc:
                return self._error(422, exc.code, str(exc))
            return self._send(201, asdict(note))

        def do_PATCH(self):
            m = _NOTE.match(self.path)
            if not m:
                return self._error(404, "not_found")
            fields = {k: v for k, v in self._json().items() if k in ("title", "body", "tags")}
            try:
                note = store.update(int(m.group(1)), **fields)
            except LimitError as exc:
                return self._error(422, exc.code, str(exc))
            return self._send(200, asdict(note)) if note else self._error(404, "not_found")

        def do_DELETE(self):
            m = _NOTE.match(self.path)
            if m and store.delete(int(m.group(1))):
                return self._send(204)
            return self._error(404, "not_found")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8500)
    args = parser.parse_args()
    store = Store(os.getenv("NOTES_DB", "notes.db"))
    ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store)).serve_forever()


if __name__ == "__main__":
    main()
