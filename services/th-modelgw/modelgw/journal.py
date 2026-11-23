from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from modelgw.context import Replacement
from modelgw.schemas import Task


@dataclass(frozen=True, slots=True)
class Invocation:
    job_id: UUID
    trace_id: UUID
    task: Task
    prompt_version: str
    rules_version: str | None
    model: str | None
    request_hash: str | None


@dataclass(frozen=True, slots=True)
class InvocationResult:
    outcome: str
    duration_ms: int
    model_version: str | None
    cached: bool
    confidence: float | None
    human_reason: str | None


class Journal(Protocol):
    async def begin(self, invocation: Invocation, replacements: tuple[Replacement, ...]) -> int: ...

    async def finish(self, attempt: int, result: InvocationResult) -> None: ...
