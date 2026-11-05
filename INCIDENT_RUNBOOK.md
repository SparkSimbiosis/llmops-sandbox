# INCIDENT_RUNBOOK: три інциденти стенда дослівними командами

| # | Сценарій | Як видно | Дія |
|---|---|---|---|
| 1 | Провайдер моделі лежить -> fallback -> відновлення | error rate, fallback, `/providers` | нічого руками: breaker + запасна модель, потім нові питання |
| 2 | Регресія якості -> відкат промпта | evals червоні, «не знаю» при HTTP 200 | `POST /prompts/{old}/activate` |
| 3 | Сплеск вартості | `/cost`, вартість на запит у `requests` | відкат промпта, роутера або реальної моделі — за причиною |

Кожен сценарій — у тому самому форматі: відтворення, **симптом**, **підтвердження**, **дія**, **перевірка
відновлення**, таймлайн і два питання розбору («як дізнались» і «що зробимо, щоб не повторилось»).

Команди виконуються **дослівно** в Git Bash (Windows) або bash (Linux/macOS) з кореня репозиторію.
Тіла запитів — латиницею (кирилиця в `curl -d` у Git Bash псується).

> **Порти.** Нижче — стандартні порти стенда: сервіс `8080`, gateway `4000`, консоль `4200`.
> Якщо стек піднято з `demo-ports.yml` (бо стандартні порти зайняті), у кроці 1 команда —
> `docker compose -f docker-compose.yml -f demo-ports.yml up --build -d`, а в усіх URL нижче
> `8080` -> `8180`, `4000` -> `4100`, `4200` -> `4300`. Інших відмінностей немає.

# Сценарій 1. Провайдер моделі лежить -> fallback -> відновлення

Відтворює інцидент «провайдер повертає 503» на mock і показує, як сервіс його переживає:
ввічлива заглушка замість помилки, перемикання на запасну модель, статус `degraded`, а після відновлення —
нормальні відповіді і спад `error_rate_pct`.

## 1. Підняти стек

```bash
docker compose up --build -d
docker compose ps
```

Очікується: 5 контейнерів `Up` — `ui`, `service`, `gateway`, `postgres`, `mock-provider`.

## 2. Дочекатися gateway і прогріти сервіс

```bash
curl -s http://localhost:4000/health/liveliness
curl -s -X POST http://localhost:8080/chat -H 'Content-Type: application/json' -d '{"message":"hello"}'
```

Перша команда — `"I'm alive!"` (якщо ні — почекати 30–60 с і повторити: LiteLLM стартує повільно).
Друга — звичайна відповідь бота. Без прогріву перші запити до холодного gateway падають і зіпсують метрики.

## 3. Базова лінія

```bash
curl -s http://localhost:8080/observability
curl -s http://localhost:8080/providers
```

Запам'ятати `error_rate_pct` і `fallback_events`. У `/providers` статус окремо для кожної моделі: `mock-mini`
відповіла на прогрів — `"ok"`; `mock-strong` і `mock` за останні 30 с ніхто не викликав — `"unknown"` (про них
свіжих даних немає, і стенд не вигадує «ok»). Якщо після кроку 2 минуло понад 30 с, `mock-mini` теж `"unknown"`.

## 4. Інцидент: провайдер повертає 503

```bash
curl -s -X POST http://localhost:8080/chat -H 'Content-Type: application/json' -d '{"message":"__fail_503 where is my order 1"}'
curl -s -X POST http://localhost:8080/chat -H 'Content-Type: application/json' -d '{"message":"__fail_503 where is my order 2"}'
curl -s -X POST http://localhost:8080/chat -H 'Content-Type: application/json' -d '{"message":"__fail_503 where is my order 3"}'
```

**Симптом:** кожна відповідь — HTTP 200 і текст «Вибачте, зараз тимчасові труднощі з обробкою запиту. Будь ласка,
спробуйте трохи пізніше.»: без цифр, без номера тікета, без «зроблено». Сервіс не віддає 500.

## 5. Підтвердження: що видно під час інциденту

```bash
curl -s http://localhost:8080/observability
curl -s http://localhost:8080/providers
```

- `fallback_events` виріс (кожен запит пробував запасну модель ланцюга);
- `error_rate_pct` > базової лінії (рядки зі `status = '503'`);
- `/providers` — `mock-mini` і `mock` `"degraded"`: запити йшли ланцюгом `mock-mini -> mock`, обидві впали по 3 рази
  й мають відкритий circuit breaker. `mock-strong` — не `"degraded"`: через неї жоден із цих запитів не йшов. У картці
  «Надійність провайдерів» дві червоні точки;
- `errors_by_reason.provider_5xx` зріс.

## 6. Дія: пауза для circuit breaker

```bash
sleep 6
```

Руками нічого не перемикаємо: заглушку і запасну модель уже ввімкнули fallback і circuit breaker.
Після 3 збоїв поспіль модель «відкрита» на 30 с, але раз на 5 с пропускається пробний запит (half-open).
6 с вистачає, щоб наступний запит став пробним.

## 7. Відновлення: НОВЕ питання (не з кешу)

```bash
curl -s -X POST http://localhost:8080/chat -H 'Content-Type: application/json' -d "{\"message\":\"I cannot login $(date +%s)\"}"
curl -s -X POST http://localhost:8080/chat -H 'Content-Type: application/json' -d "{\"message\":\"What can you do? $(date +%s)\"}"
curl -s -X POST http://localhost:8080/chat -H 'Content-Type: application/json' -d "{\"message\":\"hello again $(date +%s)\"}"
```

`$(date +%s)` робить кожне питання унікальним: інакше кеш віддав би стару відповідь, і «відновлення»
виглядало б успішним посеред інциденту. Очікується нормальна відповідь (про пароль/email, про можливості, привітання).

## 8. Перевірка відновлення

```bash
curl -s http://localhost:8080/observability
curl -s http://localhost:8080/providers
```

- `error_rate_pct` спав відносно кроку 5 (нові успішні запити розбавили збої);
- `fallback_events` не спадає ніколи — він лічильник; сплеск видно саме по ньому, а відновлення — по error rate;
- `/providers`: `mock-mini` знову `"ok"` (пробний запит half-open пройшов і закрив її breaker). Запасна `mock` ще
  `"degraded"`: її circuit breaker відкритий на 30 с, а пробний запит до неї не дійшов — нові питання обслужила
  `mock-mini`. Через 30 с:

```bash
sleep 30
curl -s -X POST http://localhost:8080/chat -H 'Content-Type: application/json' -d "{\"message\":\"hello once more $(date +%s)\"}"
curl -s http://localhost:8080/providers
```

  `mock-mini` — `"ok"` (щойно відповіла), `mock` — `"unknown"`, а не `"ok"`: вікно breaker'а минуло, але після
  інциденту її ніхто не перевіряв. Якби провайдер досі лежав, зелена картка збрехала б.

Те саме видно в консолі `http://localhost:4200`: плитки Fallback, Error rate і картка провайдерів.

### Евали після інциденту

```bash
python evals/run.py --dataset evals/golden.jsonl --threshold 15
```

Одразу після кроку 4 евали можуть бути червоні — circuit breaker ще тримає заглушку, це правильно.
Після кроку 7 — зелені (17/17).

### Таймлайн (приклад)

| Час | Подія |
|---|---|
| T+0 | провайдер почав віддавати 503 (крок 4), користувачі отримують заглушку |
| T+1 хв | плитка Error rate червона (> 5%), Fallback > 0 — інцидент помічено |
| T+2 хв | підтверджено: `/providers` — `degraded`, `errors_by_reason.provider_5xx` росте |
| T+2 хв | дія: руками нічого не перемикаємо, чекаємо пробного запиту half-open |
| T+3 хв | нові питання відповідають, `mock-mini` — `"ok"`, error rate спадає |
| T+4 хв | evals 17/17 — інцидент закрито |

### Розбір

- **Як дізнались:** червона плитка Error rate і ненульовий Fallback у консолі; користувачі бачили ввічливу заглушку,
  а не 500 — тобто скарг могло й не бути, тому сигнал має йти з метрик, а не від людей.
- **Що зробимо, щоб не повторилось:** алерт на `error_rate_pct` > 5% упродовж 5 хв (розділ «SLO і бюджет помилок»
  у README); фонова пробна перевірка запасних моделей, щоб після інциденту вони не лишалися `"unknown"`; у ланцюгу —
  запасна модель іншого провайдера (`REAL_MODEL`), бо обидві mock падають разом.

# Сценарій 2. Регресія якості -> відкат промпта

Нова версія промпта гірша за попередню. Провайдер живий, HTTP 200, error rate не росте — алерти з
сценарію 1 мовчать. Видно лише по змісту відповідей і по evals. Відкат робиться **без погоджень** (критерії —
розділ «Rollback-критерії» в README).

## 1. Відтворення: «реліз» гіршої версії

```bash
curl -s http://localhost:8080/prompts
curl -s -X POST http://localhost:8080/chat -H 'Content-Type: application/json' -d '{"message":"I cannot login"}'
curl -s -X POST http://localhost:8080/prompts/v1/activate -H 'Content-Type: application/json' -d '{"quote":"release: v1 to prod"}'
curl -s -X POST http://localhost:8080/chat -H 'Content-Type: application/json' -d '{"message":"I cannot login"}'
```

Перед «релізом» активна `v2`, і на питання про вхід — інструкція зі скидання пароля (заодно вона лягла в кеш під
ключем `v2`). Відповідь на активацію містить `"previous_version": "v2"` — це і є `{old}` для відкату.
Після активації `v1` те саме питання — вже інший ключ кешу, і модель відповідає `не знаю`.

## 2. Симптом

На звичайні питання бот відповідає `не знаю` (у реальному житті — скарги «бот став тупий» або падіння оцінки
LLM-as-judge). HTTP 200, `error_rate_pct` у `/observability` не змінився, `/providers` — без червоних точок.

## 3. Підтвердження

```bash
curl -s http://localhost:8080/prompts
python evals/run.py --dataset evals/golden.jsonl --threshold 15
docker compose exec postgres psql -U llmops -d llmops -c "SELECT version, previous_version, actor, approval_quote, activated_at FROM prompt_activations ORDER BY activated_at DESC LIMIT 3;"
docker compose exec postgres psql -U llmops -d llmops -c "SELECT prompt_version, count(*) AS n, max(created_at) AS last_at FROM requests WHERE created_at > now() - interval '15 minutes' GROUP BY prompt_version;"
```

- `/prompts` — активна `v1`;
- evals — 11/17, нижче порога 15, exit 1: спрацювала умова 1 з «Rollback-критеріїв»;
- в аудиті остання активація — `v1` з `previous_version = v2` і причиною з `quote`; час збігається з початком скарг;
- у `requests` свіжі рядки мають `prompt_version = v1` — регресія саме від промпта, не від моделі чи провайдера.

## 4. Дія: відкат на попередню версію

```bash
curl -s -X POST http://localhost:8080/prompts/v2/activate -H 'Content-Type: application/json' -d '{"quote":"rollback: evals below threshold"}'
```

Загальна форма — `POST /prompts/{old}/activate`, де `{old}` — `previous_version` з кроку 3. Погоджень не треба:
`v2` вже пройшла гейт, а відкат сам оборотний. Нову версію повертають лише через PR із зеленим гейтом і canary.

## 5. Перевірка відновлення

```bash
curl -s http://localhost:8080/prompts
curl -s -X POST http://localhost:8080/chat -H 'Content-Type: application/json' -d '{"message":"I cannot login"}'
docker compose logs --tail 3 service
python evals/run.py --dataset evals/golden.jsonl --threshold 15
```

- активна `v2`, в аудиті новий рядок `v2` з `previous_version = v1` і причиною відкату;
- на питання про вхід — знову інструкція зі скидання пароля, і в лозі сервісу `prompt_version=v2 status=200 cache=hit`:
  кеш при активації не скидається, тож відповідь `v2` з кроку 1 повернулася миттєво і без витрат;
- evals — 17/17, exit 0.

### Таймлайн (приклад)

| Час | Подія |
|---|---|
| T+0 | активовано `v1` («реліз»), відповіді стали `не знаю` |
| T+5 хв | скарги / падіння judge-оцінки; error rate при цьому в нормі |
| T+7 хв | підтверджено: evals 11/17, в аудиті остання активація — `v1` |
| T+8 хв | дія: `POST /prompts/v2/activate` черговим інженером, без погоджень |
| T+9 хв | відповіді знову по суті, частина — з теплого кешу `v2`; evals 17/17 |

### Розбір

- **Як дізнались:** не з error rate (він мовчав — HTTP 200), а зі змісту відповідей: evals, LLM-as-judge, скарги.
  Якщо ці сигнали не ганяються регулярно, регресія якості живе непоміченою до першої скарги.
- **Що зробимо, щоб не повторилось:** активація нової версії — лише через PR із зеленим eval-гейтом і canary 90/10
  (розділ «Canary-план» у README); прогін evals одразу після кожної активації; алерт на частку відповідей «не по
  суті» (`не знаю`, заглушка, «не зміг сформулювати») +2 п.п. до базової лінії.

# Сценарій 3. Сплеск вартості

Гроші витрачаються швидше за звичне. Причин три: дорожча версія промпта, роутер шле на сильну модель більше
запитів, ніж треба, або реальна модель (`REAL_MODEL`) з'їдає бюджет. Дія залежить від причини, тому спершу —
підтвердження.

## 1. Відтворення: потік запитів на сильну модель

```bash
curl -s http://localhost:8080/cost
for i in 1 2 3 4 5 6 7 8 9 10; do curl -s -X POST http://localhost:8080/chat -H 'Content-Type: application/json' -d "{\"message\":\"complaint about delivery $i $(date +%s)\"}"; echo; done
curl -s http://localhost:8080/cost
```

Слово `complaint` роутер шле на `mock-strong` (як і «скарг», «терміново», прохання повернути гроші), а
`$(date +%s)` не дає кешу їх погасити. Тікетів немає: це не прохання про повернення. Токен `mock-strong` у
`sandbox/llm/pricing.py` приблизно в 17 разів дорожчий за `mock-mini` — так само поводиться зламаний роутер,
що шле «все на сильну».

## 2. Симптом

`today_usd` у `/cost` росте помітно швидше, ніж до потоку; у консолі росте смужка плитки «Вартість сьогодні».
На mock абсолютні суми копійчані (бюджет `BUDGET_USD` = 5.0), тому дивимося на відносне: вартість одного запиту.
З реальною моделлю в лозі сервісу з'являється `budget 80% reached`.

## 3. Підтвердження: яка з трьох причин

```bash
docker compose exec postgres psql -U llmops -d llmops -c "SELECT model, prompt_version, count(*) AS n, round(sum(cost_usd), 6) AS usd, round(avg(cost_usd), 6) AS avg_usd, round(100.0 * count(*) FILTER (WHERE cost_usd = 0) / count(*), 1) AS zero_cost_pct FROM requests WHERE created_at > now() - interval '15 minutes' GROUP BY model, prompt_version ORDER BY usd DESC;"
curl -s http://localhost:8080/observability
docker compose logs service | grep "budget 80%"
```

- одна `prompt_version` має `avg_usd` вищу за попередню більш ніж на 10% -> причина — промпт;
- зросла частка `mock-strong` (або реальної моделі) у `n` при тій самій версії промпта -> причина — роутер (на стенді
  саме так: 10 рядків `mock-strong` з найбільшою `usd`);
- рядок `budget 80% reached` -> реальна модель дійшла до 80% денного `REAL_BUDGET_USD`;
- `zero_cost_pct` (відповіді з кешу й відмови на атаку йдуть з нульовою вартістю) і `cache_hit_pct` упали ->
  кеш холодний (після рестарту сервісу він порожній) — це мине саме.

## 4. Дія — за причиною

- **Промпт:** відкат без погоджень, як у сценарії 2:

```bash
curl -s -X POST http://localhost:8080/prompts/v2/activate -H 'Content-Type: application/json' -d '{"quote":"rollback: cost per request +10%"}'
```

- **Роутер:** знайти зміну в `sandbox/llm/router.py` (`git log -p -- sandbox/llm/router.py`) і відкотити її PR-ом
  (`git revert`) — через той самий eval-гейт. На стенді «зміни» немає: потік з кроку 1 закінчився — це і є дія.
- **Реальна модель:** бюджет-гард спрацює сам (80% -> дешевша реальна модель, 100% -> `mock-mini`). Якщо чекати не
  можна — перезапустити сервіс без реальної моделі (порожня змінна в оболонці перекриває `.env`):

```bash
REAL_MODEL= docker compose up -d service
```

## 5. Перевірка відновлення

```bash
curl -s -X POST http://localhost:8080/chat -H 'Content-Type: application/json' -d "{\"message\":\"What can you do? $(date +%s)\"}"
docker compose logs --tail 3 service
docker compose exec postgres psql -U llmops -d llmops -c "SELECT model, count(*) AS n, round(avg(cost_usd), 6) AS avg_usd FROM requests WHERE created_at > now() - interval '5 minutes' GROUP BY model;"
curl -s http://localhost:8080/cost
```

- звичайне питання знову йде на `mock-mini` (у лозі `model=mock-mini`);
- за останні 5 хв `avg_usd` на запит повернувся до базової лінії, нових рядків `mock-strong` немає;
- `today_usd` у `/cost` більше не росте стрибками.

### Таймлайн (приклад)

| Час | Подія |
|---|---|
| T+0 | почався потік запитів на `mock-strong` (крок 1) |
| T+10 хв | смужка «Вартість сьогодні» росте помітно швидше звичного |
| T+12 хв | підтверджено SQL: найбільша `usd` — `mock-strong` при тій самій `prompt_version` -> роутер |
| T+15 хв | дія: відкат зміни роутера PR-ом (на стенді — потік закінчився) |
| T+20 хв | нові звичайні питання — на `mock-mini`, вартість на запит у нормі |

### Розбір

- **Як дізнались:** плитка «Вартість сьогодні» і, з реальною моделлю, рядок `budget 80% reached` у лозі. Сума за
  день сама по собі — запізнілий сигнал: поки вона помітна, гроші вже витрачено.
- **Що зробимо, щоб не повторилось:** алерт на середню `cost_usd` на запит у розрізі `prompt_version` і `model`
  (+10% до базової лінії), а не лише на суму за день; тести роутера на «FAQ -> mini» (`tests/test_hw2_routing_cost.py`)
  обов'язкові в PR зі зміною маршрутів; денний бюджет-гард лишається останньою лінією захисту.
