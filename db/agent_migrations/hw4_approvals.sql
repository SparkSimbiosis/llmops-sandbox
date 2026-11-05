-- === hw4: approvals (початок) ===
-- Заявки на застосування diff агента (W4): модель пропонує -> система створює заявку -> людина вирішує ->
-- система пише нові версії файлів сайту. Адитивно й ідемпотентно; той самий блок стоїть у db/schema.sql.
CREATE TABLE IF NOT EXISTS agent_approvals (
    approval_id      UUID PRIMARY KEY,
    task_id          UUID NOT NULL REFERENCES agent_tasks (task_id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at       TIMESTAMPTZ,
    status           TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'rejected', 'stale')),
    action           TEXT NOT NULL DEFAULT 'apply_patch',
    idempotency_key  TEXT NOT NULL,                 -- sha256(дія + версії бази + sha256 diff)
    base_versions    JSONB NOT NULL,
    patch_diff       TEXT NOT NULL,
    patch_sha256     TEXT NOT NULL,
    files            JSONB NOT NULL DEFAULT '[]'::jsonb,
    reason           TEXT,
    conflicts        JSONB NOT NULL DEFAULT '[]'::jsonb
);
-- та сама пропозиція не створює другої відкритої заявки
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_approvals_pending_key ON agent_approvals (idempotency_key)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_agent_approvals_created_at ON agent_approvals (created_at);
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS approval_id UUID;
ALTER TABLE agent_site_file_versions ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'seed';
ALTER TABLE agent_site_file_versions ADD COLUMN IF NOT EXISTS source_task_id UUID;
ALTER TABLE agent_site_file_versions ADD COLUMN IF NOT EXISTS approval_id UUID;
-- === hw4 (кінець) ===
