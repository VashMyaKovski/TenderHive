from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, JsonValue

JobStatus = Literal[
    "принято",
    "обрабатывается",
    "ожидает уточнения",
    "ожидает решения человека",
    "завершено",
    "ошибка",
]


class Document(BaseModel):
    file_id: UUID
    name: str
    source: str


class Job(BaseModel):
    job_id: UUID
    idempotency_key: str
    source: Literal["mail", "telegram", "web"]
    created_at: AwareDatetime
    status: JobStatus
    kb_version: str
    lot_ids: list[str]
    documents: list[Document]
    profile_ref: str


class Explanation(BaseModel):
    steps: list[JsonValue]
    sources: list[JsonValue]


class SolverResult(BaseModel):
    job_id: UUID
    kb_version: str
    status: Literal["готово", "неполно", "решение не получено"]
    requirements: list[JsonValue]
    compliance: list[JsonValue]
    resources: dict[str, JsonValue]
    estimate: dict[str, JsonValue]
    risks: list[JsonValue]
    recommendation: Literal["участвовать", "не участвовать", "участвовать при выполнении условий"]
    explanation: Explanation
    artifacts: list[JsonValue]


class HumanDecision(BaseModel):
    decision_id: UUID
    job_id: UUID
    reason_code: Literal[
        "penalty_above_threshold",
        "low_margin",
        "resource_gap",
        "unclear_requirements",
        "no_solution",
        "final_approval",
    ]
    role: Literal["юрист", "руководитель подразделения", "служба снабжения", "тендерный специалист"]
    deadline: AwareDatetime
    timeout: bool
    decision: str
    comment: str
