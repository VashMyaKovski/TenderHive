# Рисковый агент TenderHive

`th-agents` принимает проверенный фрагмент договора, вызывает `th-modelgw` через `POST /api/v1/inference` с задачей `risk_assessment` и возвращает оценку со ссылкой на исходный фрагмент. Шлюз маскирует данные, обращается к модели и применяет числовое правило. Агент сохраняет выданные шлюзом `confidence` и `human_reason`.

## API внутри сети Compose

| Адрес относительно `http://th-agents:8000` | Назначение |
| --- | --- |
| `GET /.well-known/agent-card.json` | Agent Card с навыком `risk_assessment` |
| `POST /a2a` | A2A 1.0, JSON-RPC 2.0, метод `SendMessage` с ожиданием результата |
| `GET /schemas/risk-v1.json` | JSON Schema входа, результата и ошибки |
| `GET /healthz` | Готовность процесса принимать запросы |

Клиент отправляет заголовок `A2A-Version: 1.0` и одну структурированную часть `data` с `kind: risk_task.v1`. `task_id` принадлежит приложению, `a2a_task_id` присваивает A2A-сервер, `job_id` связывает подзадачу с тендером, а `trace_id` связывает ответ с вызовом шлюза. Проценты передаются десятичными строками. Значение `"0.15"` означает 0,15% в день. `source.url` и `source.locator` задают адрес документа и место в нём. Агент преобразует номера источников от шлюза в эти ссылки.

Завершённая задача содержит артефакт `risk_result` с `kind: risk_result.v1`. Причина согласования может быть `penalty_above_threshold`, `unclear_requirements` или `null`. Сбой возвращается как задача `FAILED` с `kind: risk_failure.v1` в `status.message`. При неверном входе отсутствующие или невалидные идентификаторы имеют значение `null`. Состояния A2A хранятся в памяти одного процесса и сбрасываются при перезапуске. Для повторной оценки клиент отправляет новое `SendMessage`.

Агент делает одну HTTP-попытку на подзадачу и передаёт признак возможности повтора вызывающему сервису.

| Коды сбоя | `retryable` |
| --- | --- |
| `invalid_request`, `invalid_context`, `unsafe_context`, `unsupported_prompt_version`, `task_disabled` | `false` |
| `provider_rejected`, `invalid_provider_response`, `invalid_gateway_response` | `false` |
| `provider_unavailable`, `provider_timeout`, `gateway_timeout`, `gateway_transport_error` | `true` |

Используйте `public_text` для проверенного публичного или предварительно обезличенного текста. Для вымышленных примеров используйте `synthetic_text`. `th-modelgw` применяет политику `data_collection=deny` к `public_text`.

## Воспроизводимый пример

В [примере запроса](examples/minsk-supply-risk.json) использован пункт 6.2 проекта договора на закупку хозяйственных товаров в Минске. Пункт задаёт пеню 0,15% за каждый день просрочки. Демонстрационный порог правила `kb-demo-v1` составляет 0,10%. [Опубликованная карточка закупки](https://www.bicotender.ru/ximiia/zakupka-xoziaistvennyx-tovarov-minsk-tender341041674.html)

Из корня репозитория соберите и запустите агента с тестовым шлюзом. Клиент из `tender_agents.demo` читает Agent Card через `a2a-sdk` и отправляет запрос по объявленному адресу. Команда завершится с кодом `0`, если агент вернул `penalty_above_threshold`.

```sh
docker compose -p tenderhive-agents-test --env-file .env.example -f compose.yaml -f services/th-agents/tests/compose.yaml up --build --wait th-agents
docker compose -p tenderhive-agents-test --env-file .env.example -f compose.yaml -f services/th-agents/tests/compose.yaml run --rm --no-deps --entrypoint python th-agents -m tender_agents.demo examples/minsk-supply-risk.json --url http://th-agents:8000 --timeout 20 --expect-reason penalty_above_threshold
docker compose -p tenderhive-agents-test --env-file .env.example -f compose.yaml -f services/th-agents/tests/compose.yaml --profile agents down
```

## Запуск с модельным шлюзом

Укажите `OPENROUTER_API_KEY` и `OPENROUTER_MODEL_TEST` в корневом `.env`. Для прогона с опубликованным фрагментом проверьте маршрут выбранной модели с `public_text` на нейтральном тексте. Модель должна быть доступна с политикой `data_collection=deny`.

```sh
docker compose up --build --wait th-modelgw th-agents
docker compose run --rm --no-deps --entrypoint python th-agents -m tender_agents.demo examples/minsk-supply-risk.json --url http://th-agents:8000 --timeout 130 --expect-reason penalty_above_threshold
```

Настройки по умолчанию для Compose описаны в [`.env.example`](.env.example). Для локальных изменений используйте `services/th-agents/.env`. Задайте `TH_AGENTS_MODEL_GATEWAY_URL` для адреса шлюза, `TH_AGENTS_MODEL_GATEWAY_TIMEOUT_SECONDS` для общей длительности вызова и `TH_AGENTS_PUBLIC_URL` для адреса, доступного A2A-клиенту. `TH_AGENTS_BIND_HOST` и `TH_AGENTS_PORT` задают адрес прослушивания. Compose публикует API агента внутри своей сети. Общий [`.dockerignore`](../../.dockerignore) действует на сборку образа из корня репозитория.

## Проверки на Python 3.14

```sh
cd services/th-agents
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest
```
