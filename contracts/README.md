# Контракты TenderHive

`Job`, `Requirement`, `SolverResult`, `Artifact` и `HumanDecision` описаны как Pydantic-модели в [models.py](models.py). JSON Schema можно получить вызовом `model_json_schema()`. В контракте используются коды статусов, причин и ролей на английском. Время передаётся в UTC.

Общий запрос агента `AgentTask` и результат `AgentResult` описаны в [agent_task.py](agent_task.py). Рисковый агент также принимает `RiskTaskV1` и возвращает `RiskResultV1` или `RiskFailureV1` по [контракту первого релиза](risk_task_v1.py). Вход и результат передаются в A2A как структурированные части `data`. [Описание API и пример](../services/th-agents/README.md)

Для новых заданий передавайте `external_ref`, `updated_at`, `trace_id` и метаданные документа `mime`, `checksum` и `storage_path`. Модель читает ранее сохранённые задания с русскоязычными статусами и старым форматом документов. `HumanDecision` описывает принятое решение. Открытые карточки имеют собственное представление в оркестраторе.

| Тема Kafka | Назначение |
| --- | --- |
| `tender.job.created` | Зарегистрировано новое задание |
| `tender.stage.done` | Завершён этап обработки задания |
| `tender.human.required` | Требуется решение человека |
| `tender.human.decided` | Решение человека зафиксировано |
| `tender.artifacts.ready` | Артефакты готовы |

Локальные темы создаются командой `docker compose run --rm kafka-init`. Ключ сообщения содержит `job_id`. Потребители должны допускать повторную доставку события.

Формат сообщений, источники и обработка повторной доставки описаны в [README оркестратора](../services/th-orchestrator/README.md).
