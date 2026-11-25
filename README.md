# TenderHive

TenderHive помогает готовить проекты предложений для тендеров в Беларуси. Система предназначена для приёма документов, извлечения требований, предварительной проверки соответствия и ресурсов, расчёта сметы и рисков, согласования и подготовки пакета предложения.

В репозитории подготовлен общий фундамент для разработки. [Карта сервисов](services/README.md) описывает границы модулей, [контракты](contracts/README.md) задают форматы обмена, а [CONTRIBUTING.md](CONTRIBUTING.md) описывает работу команды через `dev`.

## Локальный запуск

Для запуска Blazor на Windows откройте Docker Desktop, настройте OpenRouter в корневом `.env` и выполните `./Start-TenderHive.ps1` в PowerShell. Дашборд откроется на [http://127.0.0.1:8081/](http://127.0.0.1:8081/). Настройки, учётные записи для согласования и отдельный запуск для проверки ветки описаны в [инструкции стенда](infra/demo/README.md).

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

`kafka-init` создаёт темы из [контракта Kafka](contracts/README.md). Команду можно запускать повторно. Дашборд использует OpenRouter через `th-modelgw`. Подробные настройки шлюза приведены в [инструкции сервиса](services/th-modelgw/README.md).

Контролируемый регрессионный прогон `th-eval` запускается командой `docker compose --env-file .env.example run --build --rm th-eval`. Для API с сохранением прогонов в PostgreSQL запустите `docker compose --env-file .env.example up --build --wait th-eval-api`. С хоста API доступен на `http://127.0.0.1:8002`. Настройки, формат отчётов и сравнение версий описаны в [документации модуля](services/th-eval/README.md).

Пример вызова `th-agents` через A2A оценивает условие о пене из опубликованной закупки. Команды для тестового и модельного шлюза приведены в [документации агента](services/th-agents/README.md).

`th-orchestrator` проводит задание через оценку риска и согласование, восстанавливает процесс после перезапуска и ждёт подтверждённый комплект артефактов после финального утверждения. [Сценарий с PostgreSQL и тестовым шлюзом](services/th-orchestrator/README.md)

| Компонент | Адрес с хоста по умолчанию | Адрес внутри Compose |
| --- | --- | --- |
| PostgreSQL | `127.0.0.1:5432` | `postgres:5432` |
| Neo4j Bolt | `127.0.0.1:7687` | `neo4j:7687` |
| Neo4j Browser | `http://127.0.0.1:7474` | `neo4j:7474` |
| Qdrant HTTP | `http://127.0.0.1:6333` | `qdrant:6333` |
| Redis | `127.0.0.1:6379` | `redis:6379` |
| Kafka | `127.0.0.1:9092` | `kafka:29092` |

Для локального мониторинга запустите `docker compose --profile monitoring up --wait`. OpenTelemetry Collector принимает метрики и трассировки по OTLP/gRPC на `127.0.0.1:4317` и OTLP/HTTP на `127.0.0.1:4318`. Prometheus доступен на `http://127.0.0.1:9090`, Grafana с настроенным источником Prometheus — на `http://127.0.0.1:3000`. Учётные данные Grafana задаются в `.env`. Для экспорта трассировок модельного шлюза задайте `MODEL_GW_OTLP_ENDPOINT=http://otel-collector:4318/v1/traces` в `services/th-modelgw/.env`. Локальный Collector выводит сводку о полученных трассировках в журнал.

Остановить стек можно командой `docker compose --profile monitoring --profile modelgw --profile eval --profile agents --profile orchestrator down`. Именованные тома PostgreSQL, Neo4j, Qdrant, Kafka и отчётов `th-eval` сохраняются.

## Проверки

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest
docker compose --env-file .env.example --profile monitoring --profile setup --profile modelgw --profile eval --profile agents --profile orchestrator config --quiet
```
