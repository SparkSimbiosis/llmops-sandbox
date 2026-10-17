-- Базова схема. Студент розширює під власні потреби.

CREATE TABLE IF NOT EXISTS requests (
    request_id      UUID PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    model           TEXT NOT NULL,
    provider        TEXT,
    prompt_version  TEXT,
    -- яка саме версія пішла в модель: імʼя промпта в реєстрі, id рядка з prompts і sha256
    -- відправленого system-тексту (id NULL, коли реєстр порожній і спрацював дефолт — тоді prompt_version = 'none')
    prompt_name     TEXT,
    prompt_version_id UUID,
    prompt_sha256   TEXT,
    latency_ms      INTEGER,
    prompt_tokens   INTEGER,
    completion_tokens INTEGER,
    cost_usd        NUMERIC(10, 6),
    status          TEXT
);

CREATE TABLE IF NOT EXISTS prompts (
    name        TEXT NOT NULL,
    version     TEXT NOT NULL,
    body        TEXT NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- стабільний id версії: його пишемо в requests поряд із міткою v1/v2
    version_id  UUID NOT NULL DEFAULT gen_random_uuid(),
    PRIMARY KEY (name, version)
);

-- seed реєстру (W1): рівно дві версії, активна — v2. Живе тут, у міграції, а не в коді сервісу.
-- виконується лише на чистому томі: docker compose down -v && docker compose up -d
INSERT INTO prompts (name, version, body, active) VALUES
    ('support-system', 'v1', 'You are an assistant.', true),
    ('support-system', 'v2', 'You are a support assistant. Be concise and helpful.', false)
ON CONFLICT (name, version) DO NOTHING;

-- audit trail активацій (W1, опційно): хто, коли і з якої версії перемкнув.
-- рядок пишеться в тій самій транзакції, що й зміна active; на 404 рядка немає.
CREATE TABLE IF NOT EXISTS prompt_activations (
    id               BIGSERIAL PRIMARY KEY,
    name             TEXT NOT NULL,
    version          TEXT NOT NULL,
    previous_version TEXT,
    actor            TEXT NOT NULL,
    approval_quote   TEXT,  -- необовʼязкове {"quote": "..."} з тіла запиту: чиє рішення і дослівно яке
    activated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_requests_created_at ON requests (created_at);
CREATE INDEX IF NOT EXISTS idx_requests_model ON requests (model);

-- === hw1: agent (початок) ===
-- =====================================================================
-- Демо-сценарій dev-агента (не входить у контракт курсу).
-- Курсові таблиці вище (requests, prompts, prompt_activations) код
-- sandbox/agent не читає й не пише. Усі об'єкти агента мають префікс agent_.
-- Схема агента росте по тижнях: hw2-hw4 дописують адитивну DDL у кінець файлу
-- окремими блоками з маркерами (той самий текст лежить у db/agent_migrations/hwN_*.sql).
-- =====================================================================
CREATE TABLE IF NOT EXISTS agent_prompts (
    name        TEXT NOT NULL,
    version     TEXT NOT NULL,
    body        TEXT NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    version_id  UUID NOT NULL DEFAULT gen_random_uuid(),
    PRIMARY KEY (name, version)
);
-- не більше однієї активної версії на ім'я; activate = 2 оператори в одній транзакції (усі false, потім ціль true)
CREATE UNIQUE INDEX IF NOT EXISTS agent_prompts_one_active ON agent_prompts (name) WHERE active;

CREATE TABLE IF NOT EXISTS agent_prompt_activations (
    id               BIGSERIAL PRIMARY KEY,
    name             TEXT NOT NULL,
    version          TEXT NOT NULL,
    previous_version TEXT,
    actor            TEXT NOT NULL,
    approval_quote   TEXT,
    activated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_site_file_versions (
    path            TEXT NOT NULL,
    version         INTEGER NOT NULL CHECK (version >= 1),
    content         TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (path, version)          -- два apply на одній базі -> конфлікт PK -> stale (hw4)
);                                       -- source_task_id, approval_id додає міграція hw4
CREATE OR REPLACE VIEW agent_site_files_current AS
    SELECT DISTINCT ON (path) path, version, content, sha256, created_at
    FROM agent_site_file_versions ORDER BY path, version DESC;

CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id                 UUID PRIMARY KEY,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    task_text               TEXT NOT NULL,                 -- з hw4 маскований; лише синтетичні дані
    intent                  TEXT,                          -- зокрема 'site_reset' (hw4)
    mode                    TEXT NOT NULL DEFAULT 'mock' CHECK (mode IN ('mock','real')),
    base                    TEXT NOT NULL DEFAULT 'current' CHECK (base IN ('current','seed')),
    status                  TEXT NOT NULL,
    message                 TEXT,
    prompt_version          TEXT,                          -- версія agent-editor або 'none'
    context_sha256          TEXT,
    base_versions           JSONB NOT NULL DEFAULT '{}'::jsonb,
    patch_diff              TEXT,
    patch_sha256            TEXT,
    gate_report             JSONB NOT NULL DEFAULT '[]'::jsonb,
    total_prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    total_completion_tokens INTEGER NOT NULL DEFAULT 0,
    error                   TEXT
);                                                         -- complexity, total_cost_usd додає міграція hw2
CREATE INDEX IF NOT EXISTS idx_agent_tasks_created_at ON agent_tasks (created_at);

CREATE TABLE IF NOT EXISTS agent_calls (
    call_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id           UUID NOT NULL REFERENCES agent_tasks (task_id),
    seq               INTEGER NOT NULL,
    step              TEXT NOT NULL CHECK (step IN ('plan','propose')),
    provider          TEXT NOT NULL,                   -- fake | gateway | cache | fault-injector
    model             TEXT NOT NULL,
    prompt_name       TEXT,
    prompt_version    TEXT,
    prompt_sha256     TEXT,                            -- sha256 системного тексту, який справді пішов у модель
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL,                   -- '200','503','429','0',...
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (task_id, seq)
);                                                     -- cost_usd (hw2), cache_hit (hw3), attempt і fault_injected (hw4) — міграціями
CREATE INDEX IF NOT EXISTS idx_agent_calls_created_at ON agent_calls (created_at);

-- seed: промпти агента (окрема таблиця; курсова prompts завжди має рівно 2 рядки)
INSERT INTO agent_prompts (name, version, body, active) VALUES
  ('agent-planner', 'v1', 'You are the planning step of a dev agent for the demo-site project. Return a JSON plan with intent, files and complexity. Use only files from the allowed list.', true),
  ('agent-editor',  'v1', 'You are a coding assistant.', false),
  ('agent-editor',  'v2', 'You are the editing step of a dev agent for the demo-site project. Reply ONLY with a unified diff against the provided files. Touch only allowed paths. No scripts, no external URLs. If no change is needed, reply exactly NO_CHANGE.', true)
ON CONFLICT (name, version) DO NOTHING;

-- seed: файли demo-site v1; sha256 рахує Postgres (без літералів); кожен файл закінчується переведенням рядка
INSERT INTO agent_site_file_versions (path, version, content, sha256)
SELECT path, 1, content, encode(sha256(convert_to(content, 'UTF8')), 'hex')
FROM (VALUES
  ('demo-site/index.html', $f$<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8">
  <title>Demo Site</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <!-- include: header.html -->
  <main>
    <h1>Ласкаво просимо</h1>
    <p>Це навчальний фейковий сайт для демо dev-агента.</p>
  </main>
  <!-- include: footer.html -->
</body>
</html>
$f$),
  ('demo-site/header.html', $f$<header class="site-header">
  <a class="logo" href="/">Demo Site</a>
  <nav>
    <a href="/">Головна</a>
    <a href="/about">Про нас</a>
  </nav>
</header>
$f$),
  ('demo-site/footer.html', $f$<footer class="site-footer">
  <p>© 2025 Demo Site</p>
</footer>
$f$),
  ('demo-site/styles.css', $f$:root { --brand: #1d4ed8; }
body { margin: 0; font-family: system-ui, sans-serif; }
.site-header { display: flex; gap: 16px; align-items: center; padding: 12px 24px; background: var(--brand); }
.site-header a { color: #fff; text-decoration: none; }
.site-footer { padding: 12px 24px; color: #555; border-top: 1px solid #ddd; }
$f$)
) AS seed(path, content)
ON CONFLICT (path, version) DO NOTHING;
-- === hw1: agent (кінець) ===

-- === hw2: routing-cost (початок) ===
-- Складність задачі та вартість викликів dev-агента (Q-14 (б)). Адитивно й ідемпотентно;
-- той самий блок стоїть у db/schema.sql після блоку hw1.
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS complexity TEXT;
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS total_cost_usd NUMERIC(10, 6);
ALTER TABLE agent_calls ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(10, 6);
-- === hw2 (кінець) ===
