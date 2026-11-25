# th-gateway

Шлюз TenderHive на ASP.NET Core (.NET 10): точка входа для комплектов документов,
REST API для оркестратора и дашборда, авторизация и роли, контур согласования с
человеком, публикация событий задания и Telegram-бот с выдачей артефактов файлами.

Часть системы, закреплённая за ролью C# (Сушко В.Р.). Дашборд — [`th-web`](../th-web/README.md),
общие DTO обоих сервисов — [`TenderHive.Contracts`](../TenderHive.Contracts/Contracts.cs).

## 1. Состав

| Каталог | Назначение |
|---|---|
| `Program.cs`, `Configuration/` | сборка приложения, чтение настроек из окружения и `.env` |
| `Endpoints/` | REST API и политики ролей |
| `Services/` | регистрация задания, идемпотентность, правила эскалации, ссылки на артефакты |
| `Storage/` | PostgreSQL-хранилище, хранилище в памяти для демонстрации, схема БД, файлы |
| `Outbox/` | транзакционный outbox и публикация событий в Kafka |
| `Hitl/` | контроль сроков карточек согласования |
| `Telegram/` | бот, приём файлов и отправка артефактов |
| `demo/flow.sh` | сквозная проверка критериев готовности через REST API (23 проверки) |
| `demo/timeout.sh` | проверка таймаута карточки и передачи её руководителю |

## 2. Запуск

### 2.1. Compose (рабочий контур)

Из корня репозитория:

```sh
cp services/th-gateway/.env.example services/th-gateway/.env
cp services/th-web/.env.example services/th-web/.env
docker compose --profile gateway up --build --wait
```

`th-gateway` слушает `http://127.0.0.1:8080`, `th-web` — `http://127.0.0.1:8081`.
Строка подключения собирается в `compose.yaml` из общих `POSTGRES_DB`, `POSTGRES_USER` и
`POSTGRES_PASSWORD` корневого `.env`; брокер — внутренний адрес `kafka:29092`.
Схема БД создаётся шлюзом при старте, демонстрационные пользователи и версии БЗ — тоже.

### 2.2. Локальный запуск без инфраструктуры

При пустом `POSTGRES_DSN` шлюз работает на хранилище в памяти с той же семантикой
операций, при пустом `KAFKA_BROKERS` события пишутся в журнал, при пустом
`TELEGRAM_BOT_TOKEN` бот выключен. Это режим демонстрации на защите.

```sh
cp services/th-gateway/.env.example services/th-gateway/.env
dotnet run --project services/th-gateway     # http://127.0.0.1:8080
dotnet run --project services/th-web         # http://127.0.0.1:8081
```

### 2.3. Проверки

```sh
dotnet build                                  # весь solution, 0 предупреждений
bash services/th-gateway/demo/flow.sh         # сквозной сценарий, запускать из корня репозитория
```

Таймаут карточки проверяется отдельно: шлюз запускается с коротким сроком ожидания.

```sh
HITL_TIMEOUT_HOURS=0.002 dotnet run --project services/th-gateway
bash services/th-gateway/demo/timeout.sh
```

## 3. Конфигурация

Все параметры читаются из переменных окружения; файл `.env` рядом с сервисом
загружается при старте, уже заданные переменные не перетираются. Значений в коде нет:
обязательные ключи без значения останавливают запуск с явной ошибкой.

| Переменная | Назначение |
|---|---|
| `POSTGRES_DSN` | строка подключения; пусто — демонстрационное хранилище в памяти |
| `KAFKA_BROKERS` | брокеры для публикации событий outbox |
| `TELEGRAM_BOT_TOKEN` | токен Bot API; пусто — бот выключен |
| `JWT_SECRET` | ключ подписи токенов, не менее 32 символов |
| `JWT_TTL_HOURS` | срок жизни токена |
| `SERVICE_API_KEY` | ключ выдачи сервисного токена оркестратору |
| `HITL_TIMEOUT_HOURS` | срок ожидания решения по карточке |
| `HITL_SWEEP_SECONDS` | период контроля сроков |
| `ARTIFACT_BUCKET` | каталог файлов документов и артефактов |
| `ARTIFACT_LINK_TTL_MINUTES` | срок действия подписанной ссылки на файл |
| `MAX_UPLOAD_MB` | предел размера файла комплекта |
| `DEMO_PASSWORD` | пароль демонстрационных пользователей |
| `GATEWAY_PUBLIC_URL` | внешний адрес шлюза для ссылок на артефакты |
| `OUTBOX_POLL_SECONDS` | период публикации неотправленных событий |
| `DELIVERY_POLL_SECONDS` | период проверки недоставленных артефактов в Telegram |
| `REDIS_URL` | зарезервирована для оркестратора; шлюз Redis не использует |

## 4. REST-контракт

| Метод | Путь | Назначение |
|---|---|---|
| `POST` | `/api/auth/login` | вход по логину и паролю, выдача токена |
| `POST` | `/api/auth/service` | сервисный токен оркестратора по `SERVICE_API_KEY` |
| `POST` | `/api/jobs` | регистрация задания и приём файлов (multipart) |
| `GET` | `/api/jobs` | список заданий |
| `GET` | `/api/jobs/{jobId}` | статус задания и шаги обработки |
| `GET` | `/api/jobs/{jobId}/result` | результат решения |
| `GET` | `/api/jobs/{jobId}/artifacts` | артефакты с версиями |
| `GET` | `/api/jobs/{jobId}/artifacts/{artifactId}/link` | подписанная ссылка на файл |
| `GET` | `/api/jobs/{jobId}/decisions` | карточки согласования задания |
| `GET` | `/api/jobs/{jobId}/audit` | журнал аудита задания |
| `POST` | `/api/jobs/{jobId}/result` | приём результата оркестратора (multipart: часть `result` и файлы) |
| `POST` | `/api/jobs/{jobId}/steps` | отчёт о выполненном шаге обработки (сервис) |
| `GET` | `/api/decisions` | открытые карточки согласования |
| `POST` | `/api/decisions/{decisionId}` | решение по карточке |
| `GET` | `/api/kb/versions` | версии базы знаний |
| `POST` | `/api/kb/versions/{version}/activate` | активация версии и откат |
| `GET` | `/api/kb/activations` | журнал активации |
| `GET` | `/api/health` | проверка доступности |

Ответ об ошибке — явный код состояния и причина: `{"error": "...", "status": 409}`.
Имена полей JSON — `snake_case` (`job_id`, `reason_code`, `kb_version`), как в общем
контракте [`contracts/`](../../contracts/README.md).

### Идемпотентность

Ключ комплекта: `sha256(source + mailbox + message_id + sha256(файлов))`. Повторная
загрузка того же комплекта возвращает существующее задание (`200` и заголовок
`X-Idempotent-Replay: true`) и не создаёт дубликат; уникальность ключа обеспечивает
ограничение в базе.

### События

Темы совпадают с контрактом Kafka: `tender.job.created`, `tender.stage.done`,
`tender.human.required`, `tender.human.decided`, `tender.artifacts.ready`.
Событие пишется в `outbox_events` в одной транзакции с изменением состояния задания,
публикуется фоновой службой и помечается опубликованным только после подтверждения
брокера: падение сервиса не теряет событие. Ключ партиционирования — `job_id`.

## 5. Контур согласования

| Основание | Роль владельца |
|---|---|
| `penalty_above_threshold` — штраф выше порога | юрист |
| `margin_below_threshold` — маржа ниже порога | руководитель подразделения |
| `resource_gap` — неполное покрытие ресурсов | снабженец |
| `requirements_incomplete` — требования неполны или противоречивы | тендерный специалист |
| `no_solution` — решатель не нашёл решения | руководитель подразделения |
| `final_approval` — утверждение пакета заявки | руководитель подразделения |

Правила: карточка неизменяема — повторное решение отклоняется (`409`); решение принимает
владелец роли или руководитель подразделения, иначе `403`; по истечении срока карточка
переходит в состояние `timeout` и передаётся руководителю новой карточкой, автоматическое
согласие не формируется; решение фиксируется в журнале аудита.

## 6. Роли

`tender_specialist`, `procurement`, `lawyer`, `department_head`, `kb_admin`, `service`.
Демонстрационные пользователи: `specialist`, `procurement`, `lawyer`, `head`, `kbadmin`,
`orchestrator` с паролем из `DEMO_PASSWORD`.

## 7. Соответствие критериям готовности

| Критерий | Где реализовано |
|---|---|
| шлюз и дашборд поднимаются в Docker | `compose.yaml`, `services/*/Dockerfile` |
| повторная загрузка не создаёт дубликат | `Services/IdempotencyKey.cs`, `JobService.CreateJobAsync`, уникальный индекс `jobs.idempotency_key` |
| событие не теряется при падении | `Storage/Schema.sql` (`outbox_events`), `Outbox/OutboxDispatcher.cs` |
| карточка неизменяема, повторное решение отклоняется | `PostgresJobStore.ApplyDecisionAsync` (`update ... where decision = 'pending'`), ответ `409` |
| таймаут передаёт задание руководителю без автосогласия | `Hitl/DecisionTimeoutService.cs` |
| дашборд не обходит остановку | `th-web` работает только через API шлюза; кнопки «согласовать» и «отклонить» |
| артефакты выдаются файлами в Telegram | `Telegram/ArtifactNotifier.cs` |
| аудит решений | `audit_entries`, `GET /api/jobs/{jobId}/audit` |
