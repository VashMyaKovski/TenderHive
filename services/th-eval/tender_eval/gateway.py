import asyncio
from dataclasses import dataclass
from time import perf_counter
from typing import Annotated, Literal, Protocol
from uuid import UUID

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

INFERENCE_PATH = "/api/v1/inference"
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
    trace_id: UUID | None = None


class GatewayError(GatewayPayload):
    code: GatewayErrorCode
    trace_id: UUID | None = None
    answer: None = None


@dataclass(frozen=True, slots=True)
class Exchange:
    latency_ms: float
    status: int | None
    response: GatewaySuccess | GatewayError | None = None
    fault: GatewayFault | None = None


class Gateway(Protocol):
    async def infer(self, case: EvalCase, prompt_version: str) -> Exchange: ...


class HttpGateway:
    def __init__(self, client: httpx.AsyncClient, timeout: float) -> None:
        self._client = client
        self._timeout = timeout

    async def infer(self, case: EvalCase, prompt_version: str) -> Exchange:
        started = perf_counter()
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._client.post(
                    INFERENCE_PATH,
                    json={
                        "job_id": str(case.case_id),
                        "task": "risk_assessment",
                        "context": case.context,
                        "prompt_version": prompt_version,
                        "mask": case.mask,
                    },
                )
        except TimeoutError, httpx.TimeoutException:
            return Exchange((perf_counter() - started) * 1000, None, fault="transport_timeout")
        except httpx.RequestError:
            return Exchange((perf_counter() - started) * 1000, None, fault="transport_error")

        latency = (perf_counter() - started) * 1000
        try:
            if response.status_code == 200:
                payload = GatewaySuccess.model_validate_json(response.content)
                return Exchange(latency, response.status_code, payload)
            error = GatewayError.model_validate_json(response.content)
            if "answer" not in error.model_fields_set:
                return Exchange(latency, response.status_code, error)
        except ValidationError:
            pass
        return Exchange(latency, response.status_code, fault="invalid_gateway_response")
