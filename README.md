## Про проєкт: що будуємо і чому так

**Що це.** Навчальна пісочниця для практики LLMOps за програмою курсу. Домашні завдання hw1–hw6
виконуються на сервісі з контрактом стартера: `/chat`, реєстр промптів, вартість, кеш, fallback,
HITL, observability, CI-гейт. Поруч живе dev-агент-симулятор: він отримує задачу на зразок
«додай кнопку логіну в шапку», пропонує diff до фейкового навчального сайту (`demo-site/`,
файли зберігаються в БД) і застосовує зміни лише після підтвердження людиною. На агенті ті самі
механізми уроків показані вдруге, в іншому контексті.

**Чому Python 3.12 + FastAPI, а не C#.** Проєкт реалізується на реальних задачах розробки, тому сервіс
написано на стеку, яким ці задачі розв'язуються на практиці. HTTP-контракт, консоль і команди перевірки
стартера не змінюються; поведінку стартера збережено 1:1, відмінності перелічено в таблиці паритету нижче.

**Як це узгоджено з перевірками курсу.** Критерії перевірки (`homework/CHECKS.md`) не змінюються.
`evals/golden.jsonl` і `.github/workflows/eval-gate.yml` змінюються лише там, де цього вимагають
самі ДЗ: hw5 дописує до датасету 4 власні кейси (стартові 6 лишаються як є), hw6 ставить у workflow
поріг 9 і замінює коментар над ним (рядки 43-45, рядок `run:` лишається 49-м); решта workflow і job
`eval` не змінюються. Єдина буквальна команда hw1, що шукає
в `service/`, має еквівалент по фактичному коду (розділ сервісу нижче). Агент ізольований від курсових метрик:
власні таблиці `agent_*`, власні маршрути `/agent/*`, власні лічильники й кеш. Він не пише
в `requests` і не впливає на `/observability`, `/cost`, `/providers`, `/approvals` та курсові
evals. Агент працює на детермінованому фейковому провайдері, реальний ключ не потрібен.

**Як читати історію.** Кожен тиждень — окрема гілка й окремий PR; PR тижня N має базою гілку
тижня N−1, тому в diff видно лише цей тиждень. В основній ітерації тижня коміти курсу йдуть
першими, коміти агента — окремо (`agent(WN): …`); доопрацювання за зауваженнями — окремими комітами
`fix(WN)` / `agent(WN)` з рядком `Rework: mentor` після основної ітерації. Тіло кожного коміту
пояснює, що зроблено, чому так і що хочемо показати.

## Сервіс: Python sandbox

Сервіс курсу — пакет `sandbox/` (Python 3.12 + FastAPI), збирається з кореня репозиторію
файлом `Dockerfile.service`. Решта стеку стартера (`ui/`, `gateway/`, `mock-provider/`, Postgres) не змінена,
запуск той самий:

```sh
docker compose up --build
# якщо стандартні порти зайняті:
docker compose -f docker-compose.yml -f demo-ports.yml up --build -d
```

Структура:

| Шлях | Що там |
|---|---|
| `sandbox/main.py` | застосунок FastAPI; на старті до БД не ходить |
| `sandbox/course/pipeline.py` | обробник `POST /chat` — те саме місце, що `/chat` у `Program.cs`; шви тижнів позначені `# W2 routing`, `# W3 cache`, `# W4 fallback/HITL/PII`, `# W5 observability` |
| `sandbox/api/routes_course.py` | маршрути курсу у формах, які читає консоль |
| `sandbox/prompts/registry.py` | реєстр промптів (W1): активна версія, чесний дефолт `none`, активація з аудитом |
| `sandbox/llm/gateway_provider.py` | виклик LiteLLM gateway; результат — значення, а не виняток |
| `sandbox/storage/` | лінивий пул asyncpg і SQL до таблиць курсу |
| `db/schema.sql` | схема і seed реєстру (дві версії `support-system`, активна `v2`) |
| `tests/` | юніт-тести без мережі й БД (не замінюють перевірок курсу) |

### Реєстр промптів (hw1)

- Сервіс бере system-промпт з таблиці `prompts` (активна версія `support-system`) і пише в кожен рядок
  `requests` `prompt_version`, `prompt_name`, `prompt_version_id` і `prompt_sha256` — sha256 тексту, який справді
  пішов у модель.
- Немає активної версії → запасний промпт без слова «support» і `prompt_version = 'none'`: mock тоді відповідає
  «не знаю», evals червоніють — поломку реєстру видно, а не замасковано.
- Недоступна БД — не порожній реєстр: `/chat`, `GET /prompts` і активація відповідають `503`, тихого дефолту немає.
- `GET /prompts` — список версій з ознакою `active`; `POST /prompts/{version}/activate` — promote / rollback
  однією транзакцією з рядком аудиту в `prompt_activations`; неіснуюча версія → `404` без змін.
- Seed версій живе лише в `db/schema.sql`. Буквальна команда перевірки hw1 шукає в `service/`, якого тут
  немає; еквівалент по фактичному коду:
  `grep -ri "INSERT INTO prompts" sandbox/ tests/ Dockerfile.service` → порожньо, а
  `grep -c "INSERT INTO prompts" db/schema.sql` → `1`.

### Маршрутизація і вартість (hw2)

- Роутер (`sandbox/llm/router.py`): звичайні питання -> `mock-mini`; повернення, скарги, «терміново» -> `mock-strong`.
  Маркери українською й англійською: `поверн`, `терміново`, `скарг`, `refund`, `urgent`, `complaint`.
  На реальному ключі (`MODEL=gpt-5-mini` тощо) маршрут вироджується в одну модель.
- Ціни за 1k токенів (`sandbox/llm/pricing.py`):

  | Модель | Вхід, USD | Вихід, USD |
  |---|---|---|
  | `mock`, `mock-mini` | 0.00015 | 0.00060 |
  | `mock-strong` | 0.00250 | 0.01000 |

  `mock-strong` дорожча за токен приблизно в 16.7 раза. Модель без ціни -> `cost_usd = NULL`, а не 0.
- `GET /cost` -> `{today_usd, budget_usd}`: `today_usd` = `round(SUM(cost_usd), 4)` за сьогодні з `requests`,
  `budget_usd` з `BUDGET_USD` (5.0). На mock плитка консолі показує `$0.00` — вона округлює до центів.
- Запасний порядок моделей — константа `FALLBACK_CHAINS` біля роутера (`mock-strong -> mock-mini -> заглушка`);
  перемикання по ньому — у hw4.

### Таблиця паритету зі стартером (`service/Program.cs` → `sandbox/`)

| # | Стартер (C#) | Python |
|---|---|---|
| 1 | `GATEWAY_URL`, за замовчуванням `http://gateway:4000` | те саме (`sandbox/core/config.py`) |
| 2 | `DB_CONN` (рядок ADO.NET) | `DATABASE_URL` (URI: asyncpg приймає лише URI); той самий навчальний креденшел стартера |
| 3 | `MODEL`, за замовчуванням `mock` | те саме |
| 4 | тіло `{"message": ...}`; невалідне тіло → 400 | pydantic-модель; невалідне тіло → 422 (без рядка в логу) |
| 5 | GUID на запит | `uuid4()` |
| 6 | таймер на початку обробника | `perf_counter()` першою дією |
| 7 | `model = MODEL` (шов W2) | те саме, маркер `# W2 routing` |
| 8 | промпт захардкоджено | активна версія з реєстру (hw1) |
| 9 | у gateway `{model, messages: [system, user]}` без `tools` | те саме, рівно два ключі й два повідомлення |
| 10 | `POST /v1/chat/completions`, таймаут 100 с | httpx, таймаут 100 с |
| 11 | `status` — HTTP-код відповіді gateway | те саме |
| 12 | `content: null` → `""`; немає `content` чи `usage` → «Сервіс тимчасово недоступний.», `status` = HTTP-код | те саме (у логу `status = '200'`, токени 0) |
| 13 | `tool` — назва першого `tool_call`, інструмент не виконується | те саме (виконання — з hw3/hw4) |
| 14 | токени з `usage` | те саме |
| 15 | мережа / gateway недоступні → «Сервіс тимчасово недоступний.», `status` 0 або HTTP-код | те саме |
| 16 | `latency_ms` після виклику, до запису в лог | те саме |
| 17 | `cost_usd = null` | `None` до hw2 |
| 18 | один `INSERT` у `requests` до відповіді; помилка запису не валить запит | те саме: `await` до відповіді, помилка йде в лог сервісу |
| 19 | відповідь `{request_id, content, tool, latency_ms}`, 200 | те саме |
| 20 | `/health` → `{"status":"ok"}` | те саме, без звернення до БД |
| 21 | заглушки `/observability`, `/cost`, `/prompts`, `/providers`, `/approvals` — `{"todo": ...}` | дослівно ті самі тексти; кожна оживає у свій тиждень (`/prompts` — у hw1) |
| 22 | слухає `0.0.0.0:8080` | uvicorn, один процес (кеш і лічильники наступних тижнів живуть у пам'яті) |
| 23 | маршрутів активації й approve немає → 404 | те саме до свого тижня (активація — hw1, approve — hw4) |
| 24 | без CORS і auth, тіло запиту не логується | те саме |

Відмінності, видимі ззовні, лише дві: `422` замість `400` на невалідне тіло і `503`, коли БД реєстру недоступна.

## Dev-agent (поза критеріями CHECKS)

Демо-сценарій поруч із курсом: ті самі механізми уроків, показані вдруге в іншому контексті. Агент
отримує задачу на зразок «додай кнопку логіну в шапку», планує зміну і пропонує unified diff до фейкового
навчального сайту `demo-site/` (чотири файли, що зберігаються в БД). Критерії `homework/CHECKS.md` агента
не стосуються, і курсових перевірок він не зачіпає.

**Ізоляція від курсу.**
- Власні таблиці `agent_*` — блок `-- === hw1: agent ... ===` у кінці `db/schema.sql`. Курсові `requests`,
  `prompts` і `prompt_activations` агент не читає й не пише. Seed агента йде в `agent_prompts`, тому пошук
  `INSERT INTO prompts` і далі знаходить лише курсовий seed.
- Власні маршрути `/agent/*`, власний лінивий пул БД (до 3 з'єднань), власні лічильники в пам'яті.
  `/observability`, `/cost`, `/providers`, `/approvals` і курсові evals від агента не залежать.
- У `sandbox/main.py` — один guarded-блок: `AGENT_ENABLED=0` вимикає `/agent/*`, а збій імпорту агента
  не валить курс. Якщо агентських таблиць немає, `/agent/*` відповідають `503`, а `/chat` і `/health`
  працюють як завжди.
- Ізоляцію перевіряють `tests/test_isolation_static.py` (імпорти, SQL, схема, guarded-блок) і
  `tests/test_isolation_runtime.py` (прогони агента не торкаються курсових репозиторіїв, gateway і стану).

**Що вміє в hw1** — той самий механізм, що й курс: реєстр промптів і версія в кожному рядку логу.
- Реєстр агентських промптів `agent-planner` v1 і `agent-editor` v1/v2 (активна v2):
  `GET /agent/prompts`; `POST /agent/prompts/{name}/{version}/activate` — з аудитом у
  `agent_prompt_activations`, невідома версія → `404` без змін; `POST /agent/prompts/{name}/rollback` —
  повертає попередню версію, а без історії → `409`. Немає активної версії → чесний запасний промпт із
  версією `none`.
- Файли сайту: `GET /agent/site/files`, `GET /agent/site/files/{path}?version=N`.
- Задачі: `POST /agent/tasks` з тілом `{"task": "...", "mode": "mock", "base": "current" | "seed"}`
  (`mode` і `base` необов'язкові), `GET /agent/tasks`, `GET /agent/tasks/{task_id}`. Одна задача — два
  виклики моделі: `plan` (`agent-planner` обирає намір і файли) і `propose` (`agent-editor` повертає diff).
  Кожен виклик — рядок `agent_calls` з `prompt_version` і `prompt_sha256` тексту, який справді пішов у модель.
- Гейти hw1: `parse_ok` (відповідь справді є diff) і `allowed_paths` (лише чотири файли `demo-site/`, без `..`,
  абсолютних шляхів, перейменувань, створення й видалення файлів). У hw1 diff лише пропонується, сайт не змінюється.

Статуси задачі (усі з HTTP 200): `proposed`; `no_change` — сайт уже такий; `no_patch` — модель не повернула
diff (так виглядає зламаний або відкочений промпт `agent-editor`); `needs_clarification` — намір не розпізнано;
`rejected_by_gate`; `provider_error` — виклик моделі впав, змін не запропоновано. Коди помилок: `422` — невалідне
тіло, `403 {"error":"real_mode_disabled"}` — запит із `"mode":"real"`, `503` — недоступна БД агента.

**Модель.** Детермінований `FakeProvider` у процесі (`sandbox/llm/fake_provider.py`): реальний ключ не потрібен,
курсовий mock-provider не змінюється. Маркери в тексті задачі — як у mock: `__fail_503`, `__fail_429`, `__delay`,
`__garbage`. Режим `real` до hw4 вимкнено.

**Що додає hw2** — маршрутизація й вартість, як у курсі. `plan` завжди йде на `fake-mini`, `propose` — за складністю
(`simple` → `fake-mini`, `complex` → `fake-strong`; задача довша за 300 символів — `complex`). Кожен виклик має `cost_usd`
за синтетичними цінами `sandbox/agent/pricing.py` (0.003 / 0.05 за 1k, окремо від курсових), задача — `total_cost_usd`,
а `GET /agent/cost` рахує суму дня лише по `agent_calls`. Колонки додає міграція `db/agent_migrations/hw2_routing_cost.sql`.

**Спробувати** (стандартний запуск — сервіс на `:8080`, з `demo-ports.yml` — на `:8180`; кирилицю
надсилайте лише з файлу):

```sh
SVC=http://localhost:8080
curl -s $SVC/agent/prompts
curl -s -X POST $SVC/agent/tasks -H 'Content-Type: application/json' \
  --data-binary @examples/agent/task_header_login_uk.json          # -> "status":"proposed"
curl -s -X POST $SVC/agent/prompts/agent-editor/v1/activate -H 'Content-Type: application/json' -d '{}'
curl -s -X POST $SVC/agent/tasks -H 'Content-Type: application/json' \
  --data-binary @examples/agent/task_footer_year.json              # -> "status":"no_patch"
curl -s -X POST $SVC/agent/prompts/agent-editor/rollback -H 'Content-Type: application/json' -d '{}'
```

**Як росте далі.** Схема агента росте по тижнях адитивними міграціями (`db/agent_migrations/`, з hw2; той самий
текст дописується в кінець `db/schema.sql`): hw2 — маршрутизація за складністю і вартість викликів, hw3 — кеш
кроків і dry-run, hw4 — fallback, застосування diff лише через approval, PII і preview сайту, hw5 — метрики
й власні evals, hw6 — окремий CI-job.

# Starter repository

Те, що студент клонує на першому уроці й дороблює до capstone.

**➡ Повний гайд для студента (вимоги, запуск, кожна папка, troubleshooting): [GETTING_STARTED.md](GETTING_STARTED.md).**
**➡ Домашні завдання — окремий файл на кожен тиждень: [homework/](homework/README.md).**

## Що дається готовим, що добудовує студент

| Компонент | Дається готовим | Студент добудовує |
|---|---|---|
| UI (Angular) | готові в'юхи: Chat + Console/Observability | не змінює |
| App service (.NET) | skeleton | control plane: routing, fallback, cost, policies |
| Observability API (.NET) | skeleton | агрегати з Postgres (traces, p95, cost, error-taxonomy) у Console-в'юху |
| Gateway (LiteLLM) | базовий конфіг | моделі/провайдери, порядок fallback |
| Mock provider | готовий | використовує для тестів і failure-сценаріїв |
| Postgres | базова схема | розширює під logs / cost / prompt records |
| Eval runner (Python) | skeleton | dataset, graders, пороги |
| CI (GitHub Actions) | шаблон `.github/workflows/eval-gate.yml` | eval gate |
| Redis / дашборди | опційний шаблон | advanced extension |

## Запуск (мінімальний стек)

```sh
docker compose up --build
```

Піднімаються: UI (`:4200`), сервіс (`:8080`), LiteLLM (`:4000`), Postgres, mock provider.
Для mock реальні ключі не потрібні. Redis і дашборди — опційні: `docker compose --profile advanced up`.
Для оцінки якості (model-based evals): `cp gateway/.env.example gateway/.env` і заповнити ключі.
