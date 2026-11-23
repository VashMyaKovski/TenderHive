# Рисковый агент TenderHive

`th-agents` принимает проверенный фрагмент договора, вызывает `th-modelgw` через `POST /api/v1/invoke` с задачей `risk_assessment` и возвращает оценку со ссылкой на исходный фрагмент. Шлюз маскирует данные, обращается к модели NVIDIA и применяет числовое правило. Агент сохраняет выданные шлюзом `confidence` и `human_reason`.

## API внутри сети Compose

| Адрес относительно `http://th-agents:8000` | Назначение |
| --- | --- |
| `GET /.well-known/agent-card.json` | Agent Card с навыком `risk_assessment` |
| `POST /a2a` | A2A 1.0, JSON-RPC 2.0, метод `SendMessage` с ожиданием результата |
| `GET /api/v1/schemas/risk-v1.json` | JSON Schema входа, результата и ошибки |
| `GET /api/v1/schemas/agent-task.json` | JSON Schema общего задания и результата агента |
| `GET /healthz` | Готовность процесса и доступность Redis |

Клиент отправляет заголовок `A2A-Version: 1.0` и одну структурированную часть `data` с `kind: risk_task.v1`. `task_id` принадлежит приложению, `a2a_task_id` присваивает A2A-сервер, `job_id` связывает подзадачу с тендером. Переданный клиентом `trace_id` передаётся в вызов шлюза. Если поле пропущено, агент создаёт идентификатор вызова. Проценты передаются десятичными строками. Значение `"0.15"` означает 0,15% в день. `source.url` и `source.locator` задают адрес документа и место в нём.

Завершённая задача содержит артефакт `risk_result` с `kind: risk_result.v1` и ссылкой на проверенный фрагмент. Причина согласования может быть `penalty_above_threshold`, `unclear_requirements` или `null`. Сбой возвращается как задача `FAILED` с `kind: risk_failure.v1` в `status.message`.

В общем формате `AgentTask` передайте `agent_role: "risk"` и `kb_version`, а поля `fragment_id`, `source`, `fragment_text`, `fragment_type`, `penalty_daily_percent`, `penalty_daily_threshold_percent` и `prompt_version` укажите в `input`. Результат содержит `DONE` или `NEEDS_REVIEW`, основания `evidence` и версии модели и промпта в `model_calls`. Для других ролей ответ содержит `FAILED` с кодом `role_unavailable`.

Повторный `SendMessage` с тем же `task_id` и входом возвращает результат или постоянный отказ из Redis в пределах `TH_AGENTS_TASK_TTL_SECONDS`. Транспорт получает новый `a2a_task_id`, результат сохраняет `trace_id` исходной оценки. Одновременно выполняется одна оценка. Временный сбой допускает следующую попытку с тем же входом. После сбоя процесса повтор возможен по истечении аренды `TH_AGENTS_TASK_LEASE_SECONDS`. История A2A хранится в памяти процесса.

Агент делает одну HTTP-попытку на подзадачу и передаёт признак возможности повтора вызывающему сервису.

| Коды сбоя | `retryable` |
| --- | --- |
| `invalid_request`, `invalid_context`, `unsafe_context`, `unsupported_prompt_version`, `task_disabled` | `false` |
| `provider_rejected`, `invalid_provider_response`, `invalid_gateway_response` | `false` |
| `provider_unavailable`, `provider_timeout`, `gateway_timeout`, `gateway_transport_error` | `true` |
| `gateway_storage_unavailable`, `task_in_progress`, `task_store_unavailable` | `true` |
| `task_id_conflict` | `false` |

Используйте `public_text` для проверенного публичного или предварительно обезличенного текста и `synthetic_text` для вымышленных примеров.

## Воспроизводимый пример

В [примере запроса](examples/minsk-supply-risk.json) использован пункт 6.2 проекта договора на закупку хозяйственных товаров в Минске. Пункт задаёт пеню 0,15% за каждый день просрочки. Демонстрационный порог правила `kb-demo-v1` составляет 0,10%. [Опубликованная карточка закупки](https://www.bicotender.ru/ximiia/zakupka-xoziaistvennyx-tovarov-minsk-tender341041674.html)

Из корня репозитория соберите и запустите агента с тестовым шлюзом. Клиент из `tender_agents.demo` читает Agent Card через `a2a-sdk` и отправляет запрос по объявленному адресу. Команда завершится с кодом `0`, если агент вернул `penalty_above_threshold`.

```sh
docker compose -p tenderhive-agents-test --env-file .env.example -f compose.yaml -f services/th-agents/tests/compose.yaml up --build --wait th-agents
docker compose -p tenderhive-agents-test --env-file .env.example -f compose.yaml -f services/th-agents/tests/compose.yaml run --rm --no-deps --entrypoint python th-agents -m tender_agents.demo examples/minsk-supply-risk.json --url http://th-agents:8000 --timeout 20 --expect-reason penalty_above_threshold
docker compose -p tenderhive-agents-test --env-file .env.example -f compose.yaml -f services/th-agents/tests/compose.yaml --profile agents down
```

## Запуск с модельным шлюзом

Заполните настройки NVIDIA и ключ шифрования по [инструкции модельного шлюза](../th-modelgw/README.md).

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
