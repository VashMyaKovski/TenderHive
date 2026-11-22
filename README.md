# TenderHive

TenderHive помогает готовить заявки на тендеры в Беларуси. Система принимает документы, извлекает требования, проверяет допуск и ресурсы, рассчитывает смету и риски, передаёт спорные случаи на согласование и формирует пакет заявки.

В репозитории подготовлен общий фундамент для разработки. [Карта сервисов](services/README.md) описывает границы модулей, [контракты](contracts/README.md) задают форматы обмена, а [CONTRIBUTING.md](CONTRIBUTING.md) описывает работу команды через `dev`.

## Локальный запуск

Нужны Python 3.14, `uv` и Docker с Compose. Для корневых проверок достаточно Python и `uv`.

```sh
uv sync --locked --python 3.14
```

При первом запуске скопируйте [`.env.example`](.env.example) в `.env` командой `Copy-Item .env.example .env` в PowerShell или `cp .env.example .env` в Bash. В `.env` можно изменить занятые порты и задать локальные учётные данные. Пример содержит значения для разработки.

```sh
docker compose up --wait
docker compose run --rm kafka-init
docker compose ps
```

`kafka-init` создаёт пять тем из [контракта Kafka](contracts/README.md). Команду можно запускать повторно. Compose поднимает зависимости будущих сервисов. Каждый сервис добавляет свой запуск и миграции по мере реализации.

Контролируемый регрессионный прогон `th-eval` запускается командой `docker compose --env-file .env.example run --build --rm th-eval`. Настройки live-прогона, формат отчётов и сравнение версий описаны в [документации модуля](services/th-eval/README.md).

| Компонент | Адрес с хоста по умолчанию | Адрес внутри Compose |
| --- | --- | --- |
| PostgreSQL | `127.0.0.1:5432` | `postgres:5432` |
| Neo4j Bolt | `127.0.0.1:7687` | `neo4j:7687` |
| Neo4j Browser | `http://127.0.0.1:7474` | `neo4j:7474` |
| Qdrant HTTP | `http://127.0.0.1:6333` | `qdrant:6333` |
| Redis | `127.0.0.1:6379` | `redis:6379` |
| Kafka | `127.0.0.1:9092` | `kafka:29092` |

Для локального мониторинга запустите `docker compose --profile monitoring up --wait`. OpenTelemetry Collector принимает метрики по OTLP/gRPC на `127.0.0.1:4317` и OTLP/HTTP на `127.0.0.1:4318`. Prometheus доступен на `http://127.0.0.1:9090`, Grafana с настроенным источником Prometheus — на `http://127.0.0.1:3000`. Учётные данные Grafana задаются в `.env`. Инструментирование приложений и хранение трассировок добавляются вместе с сервисами.

Остановить стек можно командой `docker compose --profile monitoring down`. Именованные тома PostgreSQL, Neo4j, Qdrant и Kafka сохраняются.

## Проверки

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest
docker compose --env-file .env.example --profile monitoring --profile setup --profile eval config --quiet
```
