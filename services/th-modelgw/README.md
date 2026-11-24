# th-modelgw

Внутренний шлюз FastAPI для оценки рисков, черновиков документов, эмбеддингов и анализа предварительно обезличенных изображений. Для демонстрации используется OpenRouter, доступ к NVIDIA сохраняется для переключения через конфигурацию. Ключ выбранного провайдера передаётся по HTTPS только на соответствующий адрес API (`openrouter.ai` или `integrate.api.nvidia.com`). [API OpenRouter](https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request), [API NVIDIA](https://docs.api.nvidia.com/nim/reference/llm-apis)

## Запуск

Укажите в корневом `.env` значение `MODEL_GW_POSTGRES_DSN`. Для демонстрации задайте `MODEL_GW_PROVIDER=openrouter`, `OPENROUTER_API_KEY` и `MODEL_GW_MASKING_KEY`, затем заполните идентификаторы нужных моделей. Возьмите остальные параметры из [примера настроек сервиса](.env.example). Для ключа маскирования используйте отдельный ключ Fernet. В окружении сервиса его можно сгенерировать командой `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

```sh
docker compose --env-file .env.example --env-file .env up --build --wait th-modelgw
```

Compose запускает PostgreSQL, Redis и одноразовый `th-modelgw-migrate` перед шлюзом. При запуске без Compose настройте `.env` в каталоге сервиса и выполните `uv run --locked python -m modelgw migrate`, затем `uv run --locked python -m modelgw`. Миграция создаёт схему `modelgw`, регистрирует неизменяемые версии промптов `v1` и `v3` и допускает повторный запуск. В рабочем окружении используйте отдельную базу или отдельные разрешения на схему сервиса.

Корневой `.env.example` выбирает NVIDIA с отключёнными моделями для локальной проверки запуска без ключей. При запуске с двумя файлами окружения Compose берёт настройки демонстрации из корневого `.env`.

Шлюз доступен через `http://127.0.0.1:8001` с хоста и `http://th-modelgw:8000` в Compose. `GET /healthz` проверяет процесс, `GET /readyz` — доступность журнала и активной версии промпта, `/docs` — схему API.

- `OPENROUTER_MODEL_TEST` задаёт модель для оценки риска и черновика. `OPENROUTER_VISION_MODEL` задаёт модель с поддержкой изображений, если нужна задача `vision`. Для этих задач выберите модели с поддержкой ответа в формате JSON.
- Для демонстрационных эмбеддингов задайте `OPENROUTER_EMBEDDING_MODEL=openai/text-embedding-3-small`. При смене модели эмбеддингов используйте отдельный векторный индекс или пересчитайте сохранённые векторы выбранной моделью. [API эмбеддингов OpenRouter](https://openrouter.ai/docs/api/api-reference/embeddings/create-embeddings)
- Для NVIDIA задайте `MODEL_GW_PROVIDER=nvidia` и `NVIDIA_API_KEY` в корневом `.env`, затем пересоздайте шлюз. `MODEL_RISK` в корневом `.env` включает оценку риска и черновики. `MODEL_EMBEDDING` и `MODEL_VLM` в `services/th-modelgw/.env` включают эмбеддинги и анализ изображений. Для NVIDIA-моделей с поддержкой `input_type` укажите `passage` для индексации или `query` для запросов в `MODEL_GW_EMBEDDING_INPUT_TYPE`. Для остальных NVIDIA-моделей используйте пустое значение. [API эмбеддингов NVIDIA](https://docs.api.nvidia.com/nim/reference/nvidia-nv-embedqa-e5-v5-infer)
- Пустой идентификатор модели отключает соответствующую задачу. Когда все задачи выбранного провайдера отключены, шлюз запускается с пустыми ключами для проверки готовности и валидации запросов. Отключённая задача возвращает `503 task_disabled` после проверки версии промпта.
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

| HTTP | Ответ при отказе провайдера |
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

Redis хранит проверенные ответы с TTL и ключом `model:cache:{hash}`. Хэш учитывает провайдера, задачу, модель, версию промпта и подготовленный контекст. Шлюз повторно применяет локальное правило при попадании в кэш и обращается к выбранному провайдеру при сбое Redis. Журнал включает хэш, версии модели, промпта и правил, длительность, результат и признак попадания в кэш. Текст запроса, изображения, исходные ответы и ключи исключены из журналов и трассировок.

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

Для интеграционной проверки миграции, журнала и шифрования задайте `MODEL_GW_TEST_POSTGRES_DSN` с адресом отдельной тестовой базы PostgreSQL. Тесты HTTP используют эмуляцию ответов OpenRouter и NVIDIA.
