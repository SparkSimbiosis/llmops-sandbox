-- === hw-i1: instructions (початок) ===
-- I-1: реєстр інструкцій стенда (ролі, правила, workflow, контракти, skills) з версіями. Адитивно й ідемпотентно;
-- той самий блок стоїть у db/schema.sql після блоку hw2. Тексти потрапляють сюди лише імпортом (read-only джерело).
CREATE TABLE IF NOT EXISTS agent_instruction_items (
    kind         TEXT NOT NULL CHECK (kind IN ('role', 'rule', 'workflow', 'contract', 'skill')),
    name         TEXT NOT NULL,
    version      TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    body         TEXT NOT NULL,
    source       TEXT NOT NULL,
    source_path  TEXT NOT NULL,
    source_ref   TEXT,
    imported_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, name, version)
);
-- активна версія — на рівні проєкту; share_with_model — чи можна віддавати текст у реальну модель (R-1)
CREATE TABLE IF NOT EXISTS agent_instruction_active (
    project          TEXT NOT NULL,
    kind             TEXT NOT NULL,
    name             TEXT NOT NULL,
    version          TEXT NOT NULL,
    share_with_model BOOLEAN NOT NULL DEFAULT false,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project, kind, name),
    FOREIGN KEY (kind, name, version) REFERENCES agent_instruction_items (kind, name, version)
);
-- === hw-i1 (кінець) ===
