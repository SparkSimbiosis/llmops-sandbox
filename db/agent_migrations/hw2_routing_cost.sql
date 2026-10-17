-- === hw2: routing-cost (початок) ===
-- Складність задачі та вартість викликів dev-агента (Q-14 (б)). Адитивно й ідемпотентно;
-- той самий блок стоїть у db/schema.sql після блоку hw1.
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS complexity TEXT;
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS total_cost_usd NUMERIC(10, 6);
ALTER TABLE agent_calls ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(10, 6);
-- === hw2 (кінець) ===
