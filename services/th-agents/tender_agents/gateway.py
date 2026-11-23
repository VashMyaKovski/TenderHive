import asyncio
from collections.abc import Mapping
from typing import Annotated, Literal
from uuid import UUID, uuid4

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

type RequestError = Literal[
    "invalid_request",
    "invalid_context",
    "unsafe_context",
    "unsupported_prompt_version",
    "task_disabled",
    "storage_unavailable",
]
type ProviderReason = Literal["timeout", "auth", "rate_limit", "schema_mismatch", "unavailable"]
_STATUS: Mapping[RequestError, int] = {
    "invalid_request": 422,
    "invalid_context": 422,
    "unsafe_context": 422,
    "unsupported_prompt_version": 422,
    "task_disabled": 503,
    "storage_unavailable": 503,
}
_PROVIDER_FAILURES: Mapping[ProviderReason, tuple[int, FailureCode]] = {
    "auth": (502, "provider_rejected"),
    "rate_limit": (503, "provider_unavailable"),
    "unavailable": (503, "provider_unavailable"),
    "timeout": (504, "provider_timeout"),
    "schema_mismatch": (502, "invalid_provider_response"),
}
_RETRYABLE = frozenset(
    {
        "gateway_timeout",
        "gateway_transport_error",
        "gateway_storage_unavailable",
        "provider_unavailable",
        "provider_timeout",
        "task_in_progress",
        "task_store_unavailable",
    }
)


class GatewayFailure(Exception):
    def __init__(self, code: FailureCode, trace_id: UUID | None) -> None:
        super().__init__(code)
        self.code: FailureCode = code
        self.trace_id = trace_id

    @property
    def retryable(self) -> bool:
        return self.code in _RETRYABLE


class ErrorResponse(Contract):
    code: RequestError
    message: str
    trace_id: UUID


class UnavailableResponse(Contract):
    job_id: UUID
    status: Literal["UNAVAILABLE"]
    reason: ProviderReason
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
    source_ids: Annotated[list[Annotated[int, Field(strict=True)]], Field(min_length=1)]
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
    cached: bool
    trace_id: UUID


class ModelGateway:
    def __init__(self, client: httpx.AsyncClient, timeout_seconds: float) -> None:
        self._client = client
        self._timeout_seconds = timeout_seconds

    async def assess(self, task: RiskTaskV1, a2a_task_id: str) -> RiskResultV1:
        trace_id = task.trace_id or uuid4()
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
            "trace_id": str(trace_id),
        }
        try:
            async with asyncio.timeout(self._timeout_seconds):
                response = await self._client.post(
                    "/api/v1/invoke", json=payload, headers={"X-Trace-Id": str(trace_id)}
                )
        except TimeoutError, httpx.TimeoutException:
            raise GatewayFailure("gateway_timeout", trace_id) from None
        except httpx.RequestError:
            raise GatewayFailure("gateway_transport_error", trace_id) from None

        try:
            if response.status_code != 200:
                raise self._failure(response, task.job_id, trace_id)
            result = InferenceResponse.model_validate_json(response.content)
        except ValidationError:
            raise GatewayFailure("invalid_gateway_response", trace_id) from None

        answer = result.answer
        if (
            result.job_id != task.job_id
            or result.trace_id != trace_id
            or result.prompt_version != task.prompt_version
            or answer.rules_version != task.rules_version
            or answer.penalty_daily_percent != task.penalty_daily_percent
            or answer.penalty_daily_threshold_percent != task.penalty_daily_threshold_percent
            or any(index != 1 for index in answer.source_ids)
        ):
            raise GatewayFailure("invalid_gateway_response", trace_id)
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
            sources=[RiskSourceV1(fragment_id=task.fragment_id, source=task.source)],
        )

    @staticmethod
    def _failure(response: httpx.Response, job_id: UUID, trace_id: UUID) -> GatewayFailure:
        try:
            error = UnavailableResponse.model_validate_json(response.content)
        except ValidationError:
            problem = ErrorResponse.model_validate_json(response.content)
            if response.status_code == _STATUS[problem.code] and problem.trace_id == trace_id:
                code = (
                    "gateway_storage_unavailable"
                    if problem.code == "storage_unavailable"
                    else problem.code
                )
                return GatewayFailure(code, trace_id)
        else:
            status, code = _PROVIDER_FAILURES[error.reason]
            if (
                response.status_code == status
                and error.job_id == job_id
                and error.trace_id == trace_id
            ):
                return GatewayFailure(code, trace_id)
        return GatewayFailure("invalid_gateway_response", trace_id)
