# th-integrations — приём почты и MCP-заглушки

Почта — единственный автоматический источник заданий в MVP. Сервис опрашивает IMAP-ящик, превращает письмо с вложениями в `JOB`, публикует `tender.job.created` в Kafka и только после успешной публикации помечает письмо прочитанным. Telegram и веб-дашборд относятся к `th-gateway` и являются ручным каналом.

## API

| Метод и путь | Назначение |
| --- | --- |
| `POST /api/v1/mail/poll` | Опросить ящик; ответ содержит созданные `JOB` и коды ошибок по письмам |
| `POST /api/v1/mcp/{system}/{method}` | Вызвать MCP-заглушку: `1c.getOrganizationProfile`, `edo.getDocumentStatus`, `registry.checkCounterparty` |
| `GET /healthz` | Готовность процесса |

## Идемпотентность

Ключ — `sha256(ящик + Message-ID + sha256(вложения)…)` по отсортированным хешам вложений. Ключ проверяется до создания задания и живёт в Redis по префиксу `job:idempotency:` с TTL 7 суток (настраивается). При сбое между созданием задания и публикацией события ключ освобождается, письмо остаётся непрочитанным и будет обработано повторно. Поддерживаются PDF, DOCX, PNG, JPEG; письмо без поддерживаемых вложений получает `UNSUPPORTED_ATTACHMENT` и не создаёт `JOB`.

Задание пишется в PostgreSQL (`migrations/001_integrations.sql`), файлы — в каталог из конфигурации, событие публикуется в Kafka с ключом партиции `job_id`. Реальных обращений к 1С и ЭДО нет: заглушки возвращают фиксированные контракты, значения берутся из [`.env.example`](.env.example) с префиксом `INTEGRATIONS_`.

## Проверки

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest
```
