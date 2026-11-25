# Оркестратор TenderHive

`th-orchestrator` управляет состоянием задания, оценкой рисков через A2A 1.0, согласованием и приёмом готового пакета предложения. PostgreSQL хранит журнал карточек, этапы, входы и результаты. LangGraph сохраняет контрольные точки и продолжает прерванную оценку риска. Redis отдаёт быстрый статус и позицию задания, Kafka принимает команды и публикует события.

## API

Базовый адрес в Compose — `http://th-orchestrator:8000`. Внутренние запросы используют `Authorization: Bearer <token>` со значением `TH_ORCHESTRATOR_SERVICE_TOKEN`. Решение сотрудника требует отдельного токена из `TH_ORCHESTRATOR_REVIEWERS`, связанного с `actor_id`, ролью и разрешёнными `profile_refs`. Неверный токен даёт `401`, несовпадение роли или профиля — `403`.

| Метод и путь | Запрос и результат |
| --- | --- |
| `POST /api/v1/jobs` | `Job` регистрирует задание в ожидании нормализованного входа. Формат `{job: Job, risk: RiskTaskV1}` запускает оценку риска сразу. |
| `GET /api/v1/jobs/{job_id}` | `JobView` с фазой, этапом, `execution_id`, оценкой, карточками и подтверждёнными артефактами. |
| `POST /api/v1/jobs/{job_id}/risk-input` | `{execution_id, risk: RiskTaskV1}` принимает нормализованный фрагмент или уточнённый вход для оценки. |
| `POST /api/v1/jobs/{job_id}/solver-result` | `{execution_id, result: SolverResult}` принимает результат с совпадающими заданием и версией знаний. |
| `POST /api/v1/jobs/{job_id}/cards/{card_id}/decision` | `{decision_id, decision, comment}`. Решения `APPROVED`, `REJECTED`, `NEEDS_INFO`. Для отказа или запроса уточнения нужен комментарий. Совместимый вход `action` принимает `continue` или `stop`. |
| `POST /api/v1/jobs/{job_id}/artifacts` | `{execution_id, artifacts: Artifact[]}` принимает утверждённый пакет DOCX, PDF и XLSX от доверенного клиента. |
| `POST /api/v1/jobs/{job_id}/stage-failure` | `{execution_id, stage, failure: {code, retryable, a2a_task_id, trace_id}}` фиксирует сбой текущего внешнего этапа `extract`, `solve` или `artifacts`. |
| `POST /api/v1/jobs/{job_id}/retry-stage` | `{execution_id, stage}` возобновляет отказавший этап, если сбой допускает повтор. |
| `POST /api/v1/jobs/{job_id}/cancel` | Закрывает незавершённое задание и активную карточку. |
| `GET /healthz` | Готовность API и наличие миграций PostgreSQL. |

Для регистрации нужен статус `ACCEPTED`. Повтор исходного запроса возвращает состояние того же задания. Другие данные с тем же `job_id` или `idempotency_key` дают `409 job_conflict`. `execution_id` берётся из снимка. Повтор принятой команды с теми же данными возвращает актуальный снимок. Устаревший идентификатор или изменение уже принятого сообщения дают `409 stage_conflict`. Несоответствие результата заданию, версии знаний или метаданным даёт `422 result_mismatch`. Ошибки используют `{code, message, trace_id}`. Ответ возвращает UUID из `X-Trace-Id` запроса или выдаёт новый.

`decision_id` задаёт клиент. Идентичный повтор решения того же сотрудника возвращает сохранённое состояние. Другой ответ для закрытой карточки даёт `409 decision_conflict`. Новая запись содержит `decided_at`. Историческая запись сохраняет исходные поля и выдаётся без неизвестного времени решения.

## Переходы задания

| Событие | `phase` | `Job.status` |
| --- | --- | --- |
| Принят новый запрос | `accepted` | `ACCEPTED` |
| Запущена оценка риска | `assessing` | `PROCESSING` |
| Готово к приёму результата решателя | `ready_for_next_stage` | `PROCESSING` |
| Открыта карточка согласования | `awaiting_review` | `WAITING_HUMAN` |
| Запрошены уточнения или требуется исправленный результат | `needs_clarification` | `NEEDS_CLARIFICATION` |
| Проект утверждён, ожидается пакет | `awaiting_artifacts` | `PROCESSING` |
| Получен подтверждённый полный пакет | `done` | `DONE` |
| Отказ сотрудника, отмена или сбой этапа | `declined` или `failed` | `ERROR` |

| Причина | Получатель |
| --- | --- |
| `PENALTY_ABOVE_THRESHOLD` | `LAWYER` |
| `LOW_MARGIN` | `DIVISION_HEAD` |
| `RESOURCE_GAP` | `SUPPLY` |
| `UNCLEAR_REQUIREMENTS` | `TENDER_SPECIALIST` или `LAWYER` |
| `NO_SOLUTION` | `DIVISION_HEAD` |
| `FINAL_APPROVAL` | `TENDER_SPECIALIST` |

`RiskResultV1` сохраняет исходные коды причин. Карточка риска использует канонический код причины и роль `TH_ORCHESTRATOR_REVIEW_ROLE`. После приёма `SolverResult` карточки открываются по одной в порядке причин. Для результата `READY` оркестратор добавляет `FINAL_APPROVAL` последней. При `NEEDS_INFO` доверенный клиент передаёт пересмотренный вход для остановившегося этапа. Для `NO_SOLUTION` также требуется пересмотренный результат.

Таймаут открывает одну карточку эскалации для `DIVISION_HEAD` с исходным сроком и `timeout=true`. Продолжение требует ответа. После финального утверждения оркестратор проверяет идентификатор этапа, форматы, уникальность идентификаторов и путей, версии знаний и метаданные контрольных сумм. При наличии описаний артефактов в результате решателя квитанция должна им соответствовать. Поставщик пакета формирует, хранит и проверяет содержимое файлов.

`th-docintel`, `th-solver`, генератор документов и `th-gateway` отсутствуют в текущем наборе сервисов. При работающем `th-agents` выполняется оценка риска. Остальные этапы ждут реальных данных от доверенных поставщиков через описанные границы.

## Восстановление

`th-orchestrator-migrate` применяет SQL-миграции и миграции `AsyncPostgresSaver`. Записи по одному заданию сериализуются через PostgreSQL advisory lock. Сетевые вызовы выполняются вне SQL-транзакции. Оценка риска сохраняет число попыток и результат до дальнейшего перехода в LangGraph.

Обработчики восстанавливают незавершённые переходы и эскалируют просроченные карточки. Запись решения сохраняется до возобновления процесса. После отказа повторяется только остановившийся этап с новым `execution_id`. Квитанции обеспечивают идемпотентность повторных сообщений.

Redis хранит `job:status:{job_id}` и `checkpoint:{job_id}` с фазой, этапом, идентификатором запуска и версией позиции. Обе записи имеют TTL из `TH_ORCHESTRATOR_CHECKPOINT_TTL_SECONDS`. При опросе оркестратор обновляет новые позиции и очередную пачку активных заданий, начиная с давно обновлявшихся. После потери ключей активные позиции восстанавливаются по мере обхода. Записи завершённых и отказавших заданий истекают по TTL. Долговременное восстановление и чтение `GET` используют PostgreSQL и контрольные точки LangGraph.

## Запуск

С модельным шлюзом используйте корневой `.env` с настройками NVIDIA и запустите из корня репозитория.

```sh
docker compose up --build --wait th-modelgw th-agents th-orchestrator
```

`th-orchestrator` использует значения из [`.env.example`](.env.example) и локального `services/th-orchestrator/.env`. Compose передаёт имя базы, пользователя и пароль из настроек `postgres`. `TH_ORCHESTRATOR_AGENT_URL` указывает на тот же адрес, который агент объявляет в Agent Card. Таймауты, предел попыток, интервал обработки и размер пачки настраиваются переменными `TH_ORCHESTRATOR_*`. Для базы предусмотрены отдельные лимиты соединений бизнес-операций и чекпоинтов, каждый равен `TH_ORCHESTRATOR_DATABASE_MAX_CONNECTIONS`.

## Прогон опубликованного договора

Пункт 6.2 устанавливает пеню 0,15% в день при демонстрационном пороге 0,10%. Тестовая сеть запускает настоящий агент A2A и PostgreSQL. Предсказуемый ответ модели даёт тестовый шлюз. Первая команда клиента проверяет карточку `penalty_above_threshold`, а после перезапуска вторая продолжает тот же `Job` и проверяет сохранённый результат и одну попытку агента. [Карточка закупки](https://www.bicotender.ru/ximiia/zakupka-xoziaistvennyx-tovarov-minsk-tender341041674.html)

Из корня репозитория в PowerShell выполните следующие команды. PostgreSQL в этом профиле хранится в `tmpfs` и живёт до остановки своего контейнера.

```powershell
$hitlCompose = @('-p', 'tenderhive-hitl-test', '--env-file', '.env.example', '-f', 'compose.yaml', '-f', 'services/th-agents/tests/compose.yaml', '-f', 'services/th-orchestrator/tests/compose.yaml', '--profile', 'agents', '--profile', 'orchestrator')
$hitlDemo = @('python', '-m', 'tests.demo', '--url', 'http://th-orchestrator:8000', '--job-id', '751da55e-033e-4ad7-9993-858d44d52eb1', '--fixture', 'examples/minsk-supply-risk.json')
docker compose @hitlCompose config --quiet
docker compose @hitlCompose up --build --wait th-orchestrator
docker compose @hitlCompose run --build --rm --no-deps th-orchestrator-tests uv run --locked --no-sync pytest --with-postgres
docker compose @hitlCompose run --rm --no-deps th-orchestrator-tests @hitlDemo start
docker compose @hitlCompose restart th-orchestrator
docker compose @hitlCompose up --no-build --wait th-orchestrator
docker compose @hitlCompose run --rm --no-deps th-orchestrator-tests @hitlDemo continue
docker compose @hitlCompose down
```

Локальные проверки из каталога сервиса требуют Python 3.14. `pytest --with-postgres` добавляет интеграционные проверки к тестам контрактов и A2A-клиента.

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest
```

## Kafka

Входные сообщения принимаются как JSON-тело или конверт `{event_id, version: 1, event_type, job_id, occurred_at, payload}`. Выходные события используют этот конверт. Ключ сообщения — `job_id`.

| Тема | Направление | Данные `payload` |
| --- | --- | --- |
| `tender.job.created` | Вход | `Job` или `{job, risk}`, как в `POST /api/v1/jobs` |
| `tender.human.decided` | Вход | `{card_id, actor_id, decision: HumanDecision}` связывает решение с карточкой и разрешённым сотрудником |
| `tender.human.required` | Выход | `{card_id, job_id, step, reason_code, role, question, deadline, timeout}` |
| `tender.stage.done` | Выход | `{job_id, stage, status, ts, trace_id}` |
| `tender.artifacts.ready` | Выход | `{job_id, artifacts: Artifact[]}` |

В развёрнутой среде запись во входные темы разрешена доверенным поставщикам. Оркестратор сверяет роль, профиль, метаданные и время ответа. Входной offset подтверждается после обработки либо после записи причины отклонения в PostgreSQL. Журнал отклонений содержит тему, партицию, offset и причину. При временном отказе потребитель повторяет неподтверждённый вход.

Исходящее событие сохраняется в PostgreSQL outbox в транзакции перехода. Отметка доставки записывается после подтверждения брокера. Если процесс остановится между подтверждением и отметкой, событие может быть отправлено повторно с тем же `event_id`. Потребители используют этот идентификатор для дедупликации.

`TH_ORCHESTRATOR_KAFKA_BROKERS` задаёт список адресов брокеров, `TH_ORCHESTRATOR_KAFKA_GROUP_ID` — группу потребителей, `TH_ORCHESTRATOR_REDIS_URL` — Redis. `TH_ORCHESTRATOR_IO_TIMEOUT_SECONDS` ограничивает ожидание операций с Kafka и Redis.
