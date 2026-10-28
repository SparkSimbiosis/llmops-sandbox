-- === hw3: cache (початок) ===
-- W3: позначка виклику, на який відповів кеш кроків (виклику моделі не було, cost_usd = 0). Адитивно й ідемпотентно;
-- той самий блок стоїть у db/schema.sql після блоку hw-i1.
ALTER TABLE agent_calls ADD COLUMN IF NOT EXISTS cache_hit BOOLEAN NOT NULL DEFAULT false;
-- === hw3 (кінець) ===
