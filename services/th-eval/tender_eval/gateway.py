import asyncio
from dataclasses import dataclass
from time import perf_counter
from typing import Annotated, Literal, Protocol
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from tender_eval.cases import (
    Confidence,
    EvalCase,
    GatewayErrorCode,
    HumanReason,
    Percent,
    RiskLevel,
    SourceIndex,
    Version,
)

INFERENCE_PATH = "/api/v1/invoke"
TRACE_HEADER = "X-Trace-Id"
type UnavailableReason = Literal["timeout", "auth", "rate_limit", "schema_mismatch", "unavailable"]
UNAVAILABLE_CODES: dict[UnavailableReason, tuple[GatewayErrorCode, int]] = {
    "timeout": ("provider_timeout", 504),
    "auth": ("provider_rejected", 502),
    "rate_limit": ("provider_unavailable", 503),
    "schema_mismatch": ("invalid_provider_response", 502),
    "unavailable": ("provider_unavailable", 503),
}
PROVIDER_ERRORS = frozenset(code for code, _ in UNAVAILABLE_CODES.values())
type GatewayFault = Literal["transport_timeout", "transport_error", "invalid_gateway_response"]


class GatewayPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


class Hypothesis(GatewayPayload):
    summary: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12_000)
    ]
    risk_level: RiskLevel
    penalty_daily_percent: Percent | None
    confidence: Confidence
    sources: Annotated[tuple[SourceIndex, ...], Field(min_length=1, max_length=12)]


class RiskAnswer(GatewayPayload):
    kind: Literal["risk_assessment"]
    hypothesis: Hypothesis
    source_ids: Annotated[tuple[SourceIndex, ...], Field(max_length=12)]
    rules_version: Version
    rule_id: Literal["R-RISK-04"]
    penalty_daily_percent: Percent
    penalty_daily_threshold_percent: Percent
    threshold_exceeded: bool
    conflict: bool
    human_reason: HumanReason | None


class GatewaySuccess(GatewayPayload):
    job_id: UUID
    answer: RiskAnswer
    confidence: Confidence
    model_version: Version
    prompt_version: Version
    masked: Literal[True]
    cached: Annotated[bool, Field(strict=True)]
    trace_id: UUID


class GatewayError(GatewayPayload):
    code: GatewayErrorCode
    trace_id: UUID
    answer: None = None


class GatewayUnavailable(GatewayPayload):
    job_id: UUID
    status: Literal["UNAVAILABLE"]
    reason: UnavailableReason
    trace_id: UUID
    answer: None = None


@dataclass(frozen=True, slots=True)
class Exchange:
    latency_ms: float
    status: int | None
    response: GatewaySuccess | GatewayError | None = None
    fault: GatewayFault | None = None
    unavailable_reason: UnavailableReason | None = None


class Gateway(Protocol):
    async def infer(self, case: EvalCase, prompt_version: str) -> Exchange: ...


class HttpGateway:
    def __init__(self, client: httpx.AsyncClient, timeout: float) -> None:
        self._client = client
        self._timeout = timeout

    async def infer(self, case: EvalCase, prompt_version: str) -> Exchange:
        started = perf_counter()
        trace_id = uuid4()
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._client.post(
                    INFERENCE_PATH,
                    headers={TRACE_HEADER: str(trace_id)},
                    json={
                        "job_id": str(case.case_id),
                        "task": "risk_assessment",
                        "context": case.context,
                        "prompt_version": prompt_version,
                        "mask": case.mask,
                        "trace_id": str(trace_id),
                    },
                )
        except TimeoutError, httpx.TimeoutException:
            return Exchange((perf_counter() - started) * 1000, None, fault="transport_timeout")
        except httpx.RequestError:
            return Exchange((perf_counter() - started) * 1000, None, fault="transport_error")

        latency = (perf_counter() - started) * 1000
        if response.headers.get(TRACE_HEADER) != str(trace_id):
            return Exchange(latency, response.status_code, fault="invalid_gateway_response")
        try:
            if response.status_code == 200:
                payload = GatewaySuccess.model_validate_json(response.content)
                if payload.trace_id == trace_id:
                    return Exchange(latency, response.status_code, payload)
            else:
                try:
                    failure = GatewayUnavailable.model_validate_json(response.content)
                except ValidationError:
                    error = GatewayError.model_validate_json(response.content)
                    if (
                        "answer" not in error.model_fields_set
                        and error.trace_id == trace_id
                        and error.code not in PROVIDER_ERRORS
                    ):
                        return Exchange(latency, response.status_code, error)
                else:
                    code, status = UNAVAILABLE_CODES[failure.reason]
                    if (
                        response.status_code == status
                        and failure.job_id == case.case_id
                        and failure.trace_id == trace_id
                        and "answer" not in failure.model_fields_set
                    ):
                        return Exchange(
                            latency,
                            response.status_code,
                            GatewayError(code=code, trace_id=trace_id),
                            unavailable_reason=failure.reason,
                        )
        except ValidationError:
            pass
        return Exchange(latency, response.status_code, fault="invalid_gateway_response")
