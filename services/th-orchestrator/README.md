# Оркестратор TenderHive

`th-orchestrator` принимает `Job` с нормализованным фрагментом, выполняет оценку риска через A2A 1.0 и при необходимости ждёт решения по карточке. Граф LangGraph использует `job_id` как `thread_id`. Бизнес-состояние, карточки и чекпоинты находятся в схеме PostgreSQL `th_orchestrator`.

## API

Базовый адрес в Compose — `http://th-orchestrator:8000`. Запросы запуска и просмотра используют служебный токен, а запрос решения использует отдельный токен сотрудника.

| Метод и путь | Запрос и результат |
| --- | --- |
| `POST /api/v1/jobs` | `{job: Job, risk: RiskTaskV1}`. Пара `job_id` и версии знаний должна совпадать. Ответ содержит текущую фазу, публичный `Job`, результат оценки или сбой, число попыток и карточки. |
| `GET /api/v1/jobs/{job_id}` | Тот же снимок состояния. |
| `POST /api/v1/jobs/{job_id}/cards/{card_id}/decision` | `{decision_id, action, comment}`. Действия `continue` и `stop`. Для `stop` нужен комментарий с причиной. Ответ содержит сохранённое решение в карточке и состояние задания. |
| `GET /healthz` | Готовность API и наличие миграций в PostgreSQL. |

Для запуска отправьте `Job` в статусе `принято`. Повтор нормализованного запроса возвращает состояние того же задания. Другой запрос с тем же `job_id` или `idempotency_key` получает `409 job_conflict`. Если оценка уже выполняется в другом экземпляре, ответ может содержать промежуточную фазу. Опросите `GET` для получения итоговой фазы.

У карточки свой `card_id`, `step`, роль, причина и срок в UTC. Поле `decision` содержит публичный `HumanDecision` после принятия ответа. Для открытой или просроченной карточки это поле равно `null`. `decision_id` задаётся в запросе сотрудника. Идентичный повтор с тем же `decision_id` и тем же сотрудником возвращает сохранённое решение. Другой ответ или новый ID для закрытой карточки получает `409 decision_conflict`. Чужая карточка под указанным `job_id` получает `404`.

Внутренний тестовый клиент передаёт `Authorization: Bearer <token>`. `TH_ORCHESTRATOR_SERVICE_TOKEN` даёт доступ к запуску и просмотру. `TH_ORCHESTRATOR_REVIEWERS` сопоставляет каждый токен с `actor_id`, ролью и разрешёнными `profile_refs`. Оркестратор проверяет роль карточки и `Job.profile_ref` по этим настройкам. При неверном токене возвращается `401`, при несовпадении роли или профиля — `403`. Пример токенов предназначен для тестовой сети Compose.

## Переходы задания

| Событие | `phase` | `Job.status` | Результат |
| --- | --- | --- | --- |
| Принят новый запрос | `accepted` | `принято` | Зафиксирован вход и ключ идемпотентности |
| Запущена оценка | `assessing` | `обрабатывается` | Вызов агента с ограниченным количеством попыток |
| Оценка без причины остановки | `ready_for_next_stage` | `обрабатывается` | Следующий этап доступен |
| Требуется решение | `awaiting_review` | `ожидает решения человека` | Открыта карточка `risk_review` |
| Сотрудник выбрал `continue` | `ready_for_next_stage` | `обрабатывается` | Решение сохранено, рисковый этап завершён |
| Сотрудник выбрал `stop` | `declined` | `завершено` | Рассмотрение завершено с причиной отказа |
| `now >= deadline` для `risk_review` | `awaiting_review` | `ожидает решения человека` | Исходная карточка закрыта с `timeout=true`, открыта одна карточка `risk_escalation` для руководителя подразделения |
| Постоянный отказ агента или исчерпан лимит | `failed` | `ошибка` | Сохранён код отказа и, если доступен, `trace_id` |

| `HumanDecision.reason_code` | Точка перехода |
| --- | --- |
| `penalty_above_threshold` | Оценка риска открывает `risk_review` |
| `unclear_requirements` | Оценка риска открывает `risk_review` |
| `low_margin` | Будущее согласование сметы при низкой марже |
| `resource_gap` | Будущее согласование недоступного ресурса |
| `no_solution` | Будущая остановка после отсутствия решения |
| `final_approval` | Будущее финальное утверждение пакета заявки |

Срок и роль первой карточки задаются `TH_ORCHESTRATOR_REVIEW_TIMEOUT_SECONDS` и `TH_ORCHESTRATOR_REVIEW_ROLE`. По таймауту руководитель отвечает по новой карточке. Она хранит исходный дедлайн и `timeout=true` как данные об эскалации, остаётся открытой до решения и продолжает исходный поток.

## Восстановление

`th-orchestrator-migrate` применяет SQL-миграции и миграции `AsyncPostgresSaver`. Контейнер API проверяет их готовность. PostgreSQL сериализует записи и вызовы графа по `job_id` с помощью advisory lock на выделенном соединении. HTTP-вызов агента выполняется вне SQL-транзакции. В коротких транзакциях сохраняются карточка и статус, решение и признак незавершённого перехода либо закрытие просроченной карточки и эскалация.

Количество попыток фиксируется до HTTP-вызова. После сохранения ответа граф использует этот результат при повторном входе. Сбой процесса между отправкой HTTP-запроса и записью ответа может потребовать новой попытки в пределах сохранённого лимита. Граф сохраняет чекпоинты синхронно. Отдельный узел вызывает `interrupt` без побочных эффектов, а после `Command(resume=...)` решение читается из PostgreSQL.

При старте и периодически два фоновых обработчика проверяют незавершённые переходы и просроченные карточки. Если процесс остановился после создания карточки, повторный вход находит её по уникальному `(job_id, step)` и достраивает прерывание. Если процесс остановился после записи решения, обработчик возобновляет граф по сохранённому `decision_id`. Условные обновления открытой карточки проверяют дедлайн, а закрытие и создание эскалации выполняются одной транзакцией. [Прерывания LangGraph](https://docs.langchain.com/oss/python/langgraph/interrupts)

## Запуск

С модельным шлюзом используйте корневой `.env` с настройками OpenRouter и запустите из корня репозитория.

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

## Точки подключения событий

Для Kafka предлагается конверт с `event_id`, `version: 1`, `event_type`, `job_id`, `occurred_at` в UTC и `payload`. Ключ партиции — `job_id`. Потребители используют `event_id` для дедупликации. При подключении публикации запись события в outbox должна быть атомарна с соответствующим SQL-переходом.

| Тема | Точка графа или API | Данные `payload` |
| --- | --- | --- |
| `tender.job.created` | Вход в тот же запуск | `Job` и ссылка на нормализованный вход |
| `tender.human.required` | Создание карточки и эскалация | `card_id`, `step`, `reason_code`, `role`, `deadline`, `timeout` |
| `tender.human.decided` | Принятие ответа | `card_id` и `HumanDecision` |
| `tender.stage.done` | Завершение рискового этапа | `step`, `phase`, `trace_id`, код сбоя при отказе |
| `tender.artifacts.ready` | Будущий этап выдачи | Идентификаторы готовых артефактов |
