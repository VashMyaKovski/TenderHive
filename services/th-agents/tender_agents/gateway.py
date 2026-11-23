import asyncio
from collections.abc import Mapping
from typing import Annotated, Literal
from uuid import UUID

import httpx
from pydantic import Field, ValidationError

from contracts.risk_task_v1 import (
    Confidence,
    Contract,
    FailureCode,
    HumanReason,
    Identifier,
    Percent,
    RiskLevel,
    RiskResultV1,
    RiskSourceV1,
    RiskTaskV1,
    Text,
)

type GatewayCode = Literal[
    "invalid_request",
    "invalid_context",
    "unsafe_context",
    "unsupported_prompt_version",
    "task_disabled",
    "provider_rejected",
    "provider_unavailable",
    "provider_timeout",
    "invalid_provider_response",
]
_STATUS: Mapping[GatewayCode, int] = {
    "invalid_request": 422,
    "invalid_context": 422,
    "unsafe_context": 422,
    "unsupported_prompt_version": 422,
    "task_disabled": 503,
    "provider_rejected": 502,
    "provider_unavailable": 503,
    "provider_timeout": 504,
    "invalid_provider_response": 502,
}
_RETRYABLE = frozenset(
    {"gateway_timeout", "gateway_transport_error", "provider_unavailable", "provider_timeout"}
)


class GatewayFailure(Exception):
    def __init__(self, code: FailureCode, trace_id: UUID | None) -> None:
        super().__init__(code)
        self.code: FailureCode = code
        self.trace_id = trace_id

    @property
    def retryable(self) -> bool:
        return self.code in _RETRYABLE


class GatewayTrace(Contract):
    model_config = {"extra": "ignore", "hide_input_in_errors": True}
    trace_id: UUID


class ErrorResponse(Contract):
    code: GatewayCode
    trace_id: UUID


class Hypothesis(Contract):
    summary: Text
    risk_level: RiskLevel
    penalty_daily_percent: Percent | None
    confidence: Confidence
    sources: Annotated[
        list[Annotated[int, Field(ge=1, strict=True)]], Field(min_length=1, max_length=12)
    ]


class RiskAnswer(Contract):
    kind: Literal["risk_assessment"]
    hypothesis: Hypothesis
    source_ids: list[Annotated[int, Field(strict=True)]]
    rules_version: Identifier
    rule_id: Literal["R-RISK-04"]
    penalty_daily_percent: Percent
    penalty_daily_threshold_percent: Percent
    threshold_exceeded: bool
    conflict: bool
    human_reason: HumanReason | None


class InferenceResponse(Contract):
    job_id: UUID
    answer: RiskAnswer
    confidence: Confidence
    model_version: Identifier
    prompt_version: Identifier
    masked: Literal[True]
    trace_id: UUID


class ModelGateway:
    def __init__(self, client: httpx.AsyncClient, timeout_seconds: float) -> None:
        self._client = client
        self._timeout_seconds = timeout_seconds

    async def assess(self, task: RiskTaskV1, a2a_task_id: str) -> RiskResultV1:
        payload = {
            "job_id": str(task.job_id),
            "task": "risk_assessment",
            "context": [
                {"type": task.fragment_type, "text": task.fragment_text},
                {
                    "type": "knowledge",
                    "rules_version": task.rules_version,
                    "penalty_daily_threshold_percent": str(task.penalty_daily_threshold_percent),
                },
                {"type": "penalty", "daily_percent": str(task.penalty_daily_percent)},
            ],
            "prompt_version": task.prompt_version,
            "mask": True,
        }
        try:
            async with asyncio.timeout(self._timeout_seconds):
                response = await self._client.post("/api/v1/inference", json=payload)
        except TimeoutError, httpx.TimeoutException:
            raise GatewayFailure("gateway_timeout", None) from None
        except httpx.RequestError:
            raise GatewayFailure("gateway_transport_error", None) from None

        try:
            if response.status_code != 200:
                error = ErrorResponse.model_validate_json(response.content)
                if response.status_code == _STATUS[error.code]:
                    raise GatewayFailure(error.code, error.trace_id)
                raise GatewayFailure("invalid_gateway_response", error.trace_id)
            result = InferenceResponse.model_validate_json(response.content)
        except ValidationError:
            try:
                trace_id = GatewayTrace.model_validate_json(response.content).trace_id
            except ValidationError:
                trace_id = None
            raise GatewayFailure("invalid_gateway_response", trace_id) from None

        answer = result.answer
        if (
            result.job_id != task.job_id
            or result.prompt_version != task.prompt_version
            or answer.rules_version != task.rules_version
            or answer.penalty_daily_percent != task.penalty_daily_percent
            or answer.penalty_daily_threshold_percent != task.penalty_daily_threshold_percent
            or any(index != 1 for index in answer.source_ids)
        ):
            raise GatewayFailure("invalid_gateway_response", result.trace_id)
        return RiskResultV1(
            kind="risk_result.v1",
            task_id=task.task_id,
            job_id=task.job_id,
            a2a_task_id=a2a_task_id,
            trace_id=result.trace_id,
            rules_version=answer.rules_version,
            model_version=result.model_version,
            prompt_version=result.prompt_version,
            risk_level=answer.hypothesis.risk_level,
            summary=answer.hypothesis.summary,
            confidence=result.confidence,
            human_reason=answer.human_reason,
            sources=[RiskSourceV1(fragment_id=task.fragment_id, source=task.source)]
            if answer.source_ids
            else [],
        )
