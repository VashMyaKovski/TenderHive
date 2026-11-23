# База знаний TenderHive

`th-kb` хранит неизменяемые версии правил, фактов, справочников и тестовых примеров в PostgreSQL. Neo4j отвечает за связи между лотами, требованиями и ресурсами. Qdrant хранит векторную проекцию корпуса. Состояние активной версии хранится в PostgreSQL, а Redis можно использовать для короткого кэша графа и результатов поиска корпуса активной версии. Правила и факты читаются из PostgreSQL при каждом запросе.

## Запуск

Из корня репозитория с локальными значениями из `.env.example`:

```sh
docker compose --env-file .env.example up --build --wait th-kb th-eval-api redis
```

`th-kb-migrate` отдельно применяет SQL-миграцию, создаёт ограничение Neo4j и проверяет коллекцию Qdrant. API доступен на `127.0.0.1:8005` с хоста и на `th-kb:8000` в сети Compose. `GET /healthz` проверяет PostgreSQL, Neo4j и Qdrant. Для активации нужен отдельный API оценки `th-eval-api`.

Локальный пример настроек лежит в [`.env.example`](.env.example). Если вы меняете параметры PostgreSQL в общем `.env`, обновите `KB_POSTGRES_DSN` в `services/th-kb/.env`. `KB_EVAL_TOKEN` и пароль Neo4j передаёт корневой Compose. Для запуска вне Compose укажите адреса сервисов в `services/th-kb/.env` и выполните команды из каталога сервиса:

```sh
uv sync --locked
uv run --locked python -m tender_kb migrate
uv run --locked python -m tender_kb serve
```

`KB_EMBEDDING_MODE=fixture` использует локальные детерминированные векторы для демонстрации. В режиме `gateway` адаптер обращается к `th-modelgw` через `POST /api/v1/invoke` с задачей `embeddings`. `KB_MODEL_GW_URL`, `KB_MODEL_GW_PROMPT_VERSION` и `KB_VECTOR_SIZE` должны соответствовать настройкам шлюза. Сохраняйте профиль эмбеддингов для коллекции, пока её версии нужны для поиска и отката. Пустой `KB_REDIS_URL` отключает кэш.

## HTTP-контракт

| Запрос | Результат |
| --- | --- |
| `POST /api/v1/versions` | `201` и состояние версии. Перезагрузка идентичного снимка возвращает его текущее состояние. |
| `GET /api/v1/context` | `200` с контекстом активной версии. `version` выбирает конкретный снимок. |
| `POST /api/v1/versions/{version}/activate` | `200` с полями `version`, `status` и `report`. Итоговый статус `ACTIVE` или `REJECTED`. |
| `POST /api/v1/versions/{version}/rollback` | `200` с восстановленной версией `ACTIVE`. |

Для загрузки нужны `version`, `rules`, `facts`, `reference_data`, `test_cases` и `checksum`. Можно добавить `author`, `graph_slice` и `corpus_fragments`. `checksum` представляет SHA-256 в нижнем регистре от UTF-8 JSON без поля `checksum`. Сортируйте ключи, убирайте пробелы между элементами JSON и сохраняйте Unicode. В хеш входят только переданные поля. Даты передавайте в формате `YYYY-MM-DD`.

```python
from hashlib import sha256
import json

payload = {
    "version": "kb-demo-v1",
    "rules": [],
    "facts": [],
    "reference_data": {},
    "test_cases": [],
}
canonical = json.dumps(
    payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
)
payload["checksum"] = sha256(canonical.encode("utf-8")).hexdigest()
```

Элементы `rules` содержат `rule_id`, `condition`, `consequence`, `priority`, `scope` и номер редакции `version`. Для машинного применения условия передавайте объект `{field, op, value}` с оператором `eq`, `ne`, `lt`, `lte`, `gt` или `gte`. Следствие задаётся как `{status, reason}`, где статус принимает `MET`, `NOT_MET` или `NEEDS_REVIEW`. Область действия можно описать объектом с полями `kind`, `regime`, `task_class`. Для импорта описательных правил допускаются строки в полях `condition`, `consequence` и `scope`.

Факт содержит `fact_id`, `value`, `source`, необязательные `key` и `value_type`. У правил и фактов можно задать включительные `valid_from` и `valid_until`. `graph_slice` содержит массивы `nodes` и `edges`. У узла обязательны `node_id` и `kind`, у ребра — `source`, `target`, `relation`. У обоих объектов допустимы `attributes`. Узел может указывать `lot_id`. Фрагменты хранят `fragment_id`, `text`, `source`, `date` и необязательные `lot_ids`. Каждая ссылка на лот указывает на узел `kind=lot` в том же снимке. Тестовый пример состоит из `case_id`, `input` и `expected`.

Контекст содержит `version`, `rules`, `facts`, `reference_data`, `graph_slice` и `corpus_fragments`. Поля `regime` и `task_class` сужают выборку правил по области действия. `mode` служит альтернативным именем `task_class`. Последовательность `lot_ids=lot-1&lot_ids=lot-2` выбирает лоты, связанные с ними узлы и их соседей на расстоянии одного ребра. В корпус входят фрагменты выбранных лотов и общие фрагменты. `query` включает векторный поиск. `as_of` фильтрует периоды действия правил и фактов относительно даты задания. Без даты выдаются все периоды, чтобы вызывающий сервис мог применить дату задания самостоятельно.

## Активация и откат

Сначала сервис проверяет ссылки, типы значений, интервалы действия и пересечения правил. После записи отдельной проекции он переводит версию из `DRAFT` в `CHECK` и вызывает `POST /api/v1/kb/check` в `th-eval`. Запрос передаёт `candidate_version`, `baseline_version`, `test_set_version`, `test_cases` и заголовок `X-KB-Eval-Token`. Тот же заголовок позволяет доверенному внутреннему клиенту получить контекст конкретной версии `CHECK`. Ожидаемый ответ оценки содержит булево `passed` и массив `report` с полями `code` и `message`.

При успешной оценке одна транзакция обновляет активную версию, переводит предыдущую в `OLD` и записывает журнал. Если активная версия изменилась во время оценки, активация отвечает `409`, а повтор запроса оценит кандидата относительно нового состояния. При недоступности оценки API отвечает `503`, сохраняя кандидата в `CHECK` для повторной попытки. Проекция версии `OLD` остаётся доступна заданиям, использующим её явно. Откат атомарно восстанавливает ранее активную версию. В редком случае обрыва соединения после записи проекции может остаться незадействованная копия. Активный снимок в PostgreSQL определяет, какая проекция доступна читателям.

## Проверки

Из каталога `services/th-kb`:

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest
docker build --target test -t tenderhive-th-kb-test .
docker run --rm tenderhive-th-kb-test
```

Для проверки PostgreSQL, Neo4j, Qdrant и Redis запустите пустой локальный Compose-проект `tenderhive-kb-test` с `th-kb` и `redis`, затем выполните из каталога сервиса:

```sh
docker run --rm --network tenderhive-kb-test_default --env-file .env.example -e KB_RUN_INTEGRATION=true tenderhive-th-kb-test
```

Интеграционный тест использует эмулятор HTTP-контракта `th-eval` и проверяет чтение `CHECK`, повтор после сбоя, регрессию, конкурентный откат и изоляцию снимков.
