from typing import Literal
from uuid import UUID

from pydantic import JsonValue

from contracts.risk_task_v1 import Contract, Identifier

type AgentRole = Literal[
    "risk", "legal", "resource", "estimate", "supply", "extraction", "document"
]


class AgentTask(Contract):
    task_id: UUID
    job_id: UUID
    agent_role: AgentRole
    input: dict[str, JsonValue]
    kb_version: Identifier
    trace_id: UUID


class ModelCall(Contract):
    trace_id: UUID
    model_version: Identifier
    prompt_version: Identifier


class AgentResult(Contract):
    task_id: UUID
    job_id: UUID
    agent_role: AgentRole
    status: Literal["DONE", "NEEDS_REVIEW", "FAILED"]
    result: dict[str, JsonValue]
    evidence: list[dict[str, JsonValue]]
    model_calls: list[ModelCall]
    trace_id: UUID
