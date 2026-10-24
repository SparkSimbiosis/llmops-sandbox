# INCIDENT_RUNBOOK: провайдер моделі лежить -> fallback -> відновлення

Сценарій відтворює інцидент «провайдер повертає 503» на mock і показує, як сервіс його переживає:
ввічлива заглушка замість помилки, перемикання на запасну модель, статус `degraded`, а після відновлення —
нормальні відповіді і спад `error_rate_pct`.

Команди виконуються **дослівно** в Git Bash (Windows) або bash (Linux/macOS) з кореня репозиторію.
Тіла запитів — латиницею (кирилиця в `curl -d` у Git Bash псується).

> **Порти.** Нижче — стандартні порти стенда: сервіс `8080`, gateway `4000`, консоль `4200`.
> Якщо стек піднято з `demo-ports.yml` (бо стандартні порти зайняті), у кроці 1 команда —
> `docker compose -f docker-compose.yml -f demo-ports.yml up --build -d`, а в усіх URL нижче
> `8080` -> `8180`, `4000` -> `4100`, `4200` -> `4300`. Інших відмінностей немає.

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

Запам'ятати `error_rate_pct` і `fallback_events`. У `/providers` усі статуси `"ok"`.

## 4. Інцидент: провайдер повертає 503

```bash
curl -s -X POST http://localhost:8080/chat -H 'Content-Type: application/json' -d '{"message":"__fail_503 where is my order 1"}'
curl -s -X POST http://localhost:8080/chat -H 'Content-Type: application/json' -d '{"message":"__fail_503 where is my order 2"}'
curl -s -X POST http://localhost:8080/chat -H 'Content-Type: application/json' -d '{"message":"__fail_503 where is my order 3"}'
```

Кожна відповідь — HTTP 200 і текст «Вибачте, зараз тимчасові труднощі з обробкою запиту. Будь ласка, спробуйте трохи
пізніше.»: без цифр, без номера тікета, без «зроблено». Сервіс не віддає 500.

## 5. Що видно під час інциденту

```bash
curl -s http://localhost:8080/observability
curl -s http://localhost:8080/providers
```

- `fallback_events` виріс (кожен запит пробував запасну модель ланцюга);
- `error_rate_pct` > базової лінії (рядки зі `status = '503'`);
- `/providers` — усі `"degraded"` (3 повні відмови ланцюга поспіль), картка «Надійність провайдерів» у консолі червона;
- `errors_by_reason.provider_5xx` зріс.

## 6. Пауза для circuit breaker

```bash
sleep 6
```

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

## 8. Перевірити відновлення

```bash
curl -s http://localhost:8080/observability
curl -s http://localhost:8080/providers
```

- `error_rate_pct` спав відносно кроку 5 (нові успішні запити розбавили збої);
- `fallback_events` не спадає ніколи — він лічильник; сплеск видно саме по ньому, а відновлення — по error rate;
- `/providers`: основна модель знову `"ok"` (перша справжня відповідь скидає серію збоїв). Запасна модель ланцюга
  (`mock`) може ще бути `"degraded"`: її circuit breaker відкритий на 30 с, а пробний запит до неї не дійшов — нові
  питання обслужила основна модель. Це чесно: про запасну модель ми ще не знаємо, що вона ожила. Через 30 с:

```bash
sleep 30
curl -s http://localhost:8080/providers
```

  усі `"ok"`, картка в консолі зелена.

Те саме видно в консолі `http://localhost:4200`: плитки Fallback, Error rate і картка провайдерів.

## Додаток: евали після інциденту

```bash
python evals/run.py --dataset evals/golden.jsonl --threshold 12
```

Одразу після кроку 4 евали можуть бути червоні — circuit breaker ще тримає заглушку, це правильно.
Після кроку 7 — зелені (13/13).
