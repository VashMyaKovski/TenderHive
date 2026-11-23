# th-modelgw

Внутренний шлюз FastAPI к NVIDIA. Поддерживает оценку рисков, черновики документов, эмбеддинги и анализ предварительно обезличенных изображений. Ключ NVIDIA передаётся только по HTTPS на `integrate.api.nvidia.com`. [API NVIDIA](https://docs.api.nvidia.com/nim/reference/llm-apis)

## Запуск

Укажите в корневом `.env` значение `MODEL_GW_POSTGRES_DSN`. Для вызова модели настройте её идентификатор, `NVIDIA_API_KEY` и `MODEL_GW_MASKING_KEY`. Возьмите остальные параметры из [примера настроек сервиса](.env.example). Ключ маскирования — отдельный ключ Fernet. В окружении сервиса его можно сгенерировать командой `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

```sh
docker compose --env-file .env.example --env-file .env up --build --wait th-modelgw
```

Compose запускает PostgreSQL, Redis и одноразовый `th-modelgw-migrate` перед шлюзом. При запуске без Compose настройте `.env` в каталоге сервиса и выполните `uv run --locked python -m modelgw migrate`, затем `uv run --locked python -m modelgw`. Миграция создаёт схему `modelgw`, регистрирует неизменяемые версии промптов `v1` и `v3` и допускает повторный запуск. В рабочем окружении используйте отдельную базу или отдельные разрешения на схему сервиса.

Шлюз доступен через `http://127.0.0.1:8001` с хоста и `http://th-modelgw:8000` в Compose. `GET /healthz` проверяет процесс, `GET /readyz` — доступность журнала и активной версии промпта, `/docs` — схему API.

- `MODEL_RISK` используется для оценки риска и черновика. Модели чата и vision должны отвечать JSON в содержимом сообщения.
- `MODEL_EMBEDDING` и `MODEL_VLM` включают соответствующие задачи. Для моделей эмбеддингов, использующих `input_type`, укажите `passage` для индексации или `query` для запросов в `MODEL_GW_EMBEDDING_INPUT_TYPE`. Для остальных моделей оставьте значение пустым. [API эмбеддингов NVIDIA](https://docs.api.nvidia.com/nim/reference/nvidia-nv-embedqa-e5-v5-infer)
- При явно пустых `MODEL_RISK`, `MODEL_EMBEDDING` и `MODEL_VLM` шлюз запускается с пустыми ключами для проверки готовности и валидации запросов. Задачи в этой конфигурации отключены и возвращают `503 task_disabled` после проверки версии промпта.
- `MODEL_GW_PROMPT_VERSION` выбирает активную версию. Можно задать `PROMPT_VERSION` в окружении вместо него.
- `MODEL_GW_OTLP_ENDPOINT` включает экспорт трассировок OTLP/HTTP. Шлюз продолжает W3C `traceparent`, если он передан, и сохраняет UUID запроса в атрибуте `tender.trace_id`.

## Контракт

`POST /api/v1/invoke`

```json
{
  "job_id": "d948fa35-8771-49da-aebb-282049356694",
  "task": "risk_assessment",
  "context": [
    {"type": "synthetic_text", "text": "The delivery date is specified in the public notice."},
    {"type": "knowledge", "rules_version": "kb-v1", "penalty_daily_threshold_percent": "0.1"},
    {"type": "penalty", "daily_percent": "0.15"}
  ],
  "prompt_version": "v1",
  "mask": true,
  "trace_id": "8e5d7f65-5d17-4138-a335-af8e6cfc0601"
}
```

Успешный ответ содержит `job_id`, `answer`, `confidence`, `model_version`, `prompt_version`, `masked`, `cached` и исходный `trace_id`. Если запрос содержит `X-Trace-Id`, он должен совпадать с `trace_id`. Шлюз также возвращает `X-Trace-Id`. Входные ошибки возвращают `code`, `message` и `trace_id`.

| HTTP | Ответ при отказе NVIDIA |
| --- | --- |
| 502 | `auth` или `schema_mismatch` |
| 503 | `rate_limit` или `unavailable` |
| 504 | `timeout` |

В этих случаях возвращаются `job_id`, `status: "UNAVAILABLE"`, `reason` и `trace_id`. Совместимый маршрут `POST /api/v1/inference` сохраняет прежний формат, в котором `trace_id` входного тела отсутствует, а успех возвращается без `cached`.

### Контекст и маскирование

- `public_text` предназначен для открытого или предварительно обезличенного текста. Шлюз дополнительно маскирует распознаваемые персональные, банковские, регистрационные, организационные и коммерческие данные. При обнаружении остаточных подозрительных шаблонов возвращается `422 unsafe_context`. `synthetic_text` проходит ту же обработку.
- `sensitive_text` с категорией `personal`, `organization`, `registration`, `banking` или `commercial` исключается из запроса к модели. Шлюз сохраняет карту замен в зашифрованном виде вместе с записью вызова и идентификатором задания в PostgreSQL. При невозможности записать начало вызова шлюз возвращает `503 storage_unavailable`.
- Для `risk_assessment` укажите один элемент `knowledge` и один `penalty`. Шлюз проверяет их локально по правилу `R-RISK-04`. Модель возвращает гипотезу со ссылками на публичные фрагменты. При конфликте с правилом итоговая уверенность равна нулю. `answer.human_reason` передаёт `penalty_above_threshold` или `unclear_requirements` для проверки человеком.
- Для `document_draft` и `embeddings` нужен хотя бы один публичный или синтетический фрагмент. Векторы возвращаются в порядке входа. `confidence=1` для эмбеддингов означает, что их количество и размерность прошли проверку.
- Для `vision` нужен один `public_image` с полем `data` в виде base64 PNG или JPEG. Вызывающий сервис обезличивает пиксели до отправки. Шлюз принимает до 2 МБ и 4 млн пикселей и перекодирует изображение без метаданных перед передачей. Дополнительный публичный текст описывает задачу.

Redis хранит проверенные ответы с TTL и ключом `model:cache:{hash}`. Хэш учитывает задачу, модель, версию промпта и подготовленный контекст. Шлюз повторно применяет локальное правило при попадании в кэш и обращается к NVIDIA при сбое Redis. Журнал включает хэш, версии модели, промпта и правил, длительность, результат и признак попадания в кэш. Текст запроса, изображения, исходные ответы и ключи исключены из журналов и трассировок.

## Проверки

Из каталога сервиса:

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest
docker build --target test -t tenderhive-modelgw-test .
docker run --rm tenderhive-modelgw-test
```

Для интеграционной проверки миграции, журнала и шифрования задайте `MODEL_GW_TEST_POSTGRES_DSN` с адресом отдельной тестовой базы PostgreSQL. Тесты HTTP используют эмуляцию ответа NVIDIA.
