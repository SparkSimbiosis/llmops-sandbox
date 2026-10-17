"""Міграція агента hw2 (5.4): файл db/agent_migrations/hw2_routing_cost.sql дослівно стоїть у кінці
db/schema.sql після блоку hw1, у маркерах тижня, і містить лише адитивні ідемпотентні оператори."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "db" / "schema.sql"
MIG = ROOT / "db" / "agent_migrations" / "hw2_routing_cost.sql"
BEGIN, END = "-- === hw2: routing-cost (початок) ===", "-- === hw2 (кінець) ==="
HW1_END = "-- === hw1: agent (кінець) ==="


def statements(sql: str) -> list[str]:
    code = re.sub(r"--[^\n]*", "", sql)
    return [" ".join(s.split()) for s in code.split(";") if s.strip()]


def test_migration_file_has_week_markers():
    body = MIG.read_bytes().decode("utf-8")
    assert "\r" not in body
    lines = body.splitlines()
    assert lines[0] == BEGIN and lines[-1] == END and body.endswith("\n")


def test_migration_is_byte_identical_block_in_schema():
    schema, body = SCHEMA.read_bytes().decode("utf-8"), MIG.read_bytes().decode("utf-8")
    assert schema.count(BEGIN) == 1 and schema.count(END) == 1
    b = schema.index(BEGIN)
    assert schema[b:schema.index(END) + len(END) + 1] == body
    assert schema.index(HW1_END) < b                               # після блоку hw1
    tail = schema[b + len(body):].strip()
    assert tail == "" or tail.startswith("-- === hw")             # далі — лише блоки наступних тижнів


def test_migration_is_additive_and_idempotent():
    stmts = statements(MIG.read_text(encoding="utf-8"))
    allowed = re.compile(r"ALTER TABLE (agent_\w+) ADD COLUMN IF NOT EXISTS (\w+) ([A-Z]+(?:\(\d+, \d+\))?)")
    cols = []
    for s in stmts:
        m = allowed.fullmatch(s)
        assert m, s
        assert not re.search(r"REFERENCES|CHECK|NOT NULL|DROP|RENAME", s), s
        cols.append(m.groups())
    assert cols == [("agent_tasks", "complexity", "TEXT"), ("agent_tasks", "total_cost_usd", "NUMERIC(10, 6)"),
                    ("agent_calls", "cost_usd", "NUMERIC(10, 6)")]
