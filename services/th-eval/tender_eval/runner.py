import asyncio
from typing import Literal
from uuid import UUID

from tender_eval.cases import (
    Contract,
    ErrorExpectation,
    EvalCase,
    GatewayErrorCode,
    HumanReason,
    RiskExpectation,
    RiskLevel,
    Stratum,
)
from tender_eval.gateway import (
    Exchange,
    Gateway,
    GatewayError,
    GatewayFault,
    GatewaySuccess,
    UnavailableReason,
)

type Issue = Literal[
    "transport_timeout",
    "transport_error",
    "invalid_gateway_response",
    "missing_trace_id",
    "unexpected_success",
    "unexpected_error",
    "wrong_error",
    "wrong_job",
    "wrong_prompt_version",
    "wrong_rules",
    "wrong_penalty",
    "wrong_threshold_result",
    "wrong_conflict",
    "wrong_human_reason",
    "wrong_confidence",
    "invalid_sources",
    "missing_expected_source",
]


class Prediction(Contract):
    level: RiskLevel
    model_confidence: float
    effective_confidence: float
    model_version: str
    prompt_version: str
    rules_version: str
    threshold_exceeded: bool
    conflict: bool
    human_reason: HumanReason | None
    source_ids: tuple[int, ...]


class Observation(Contract):
    case_id: UUID
    stratum: Stratum
    repeat: int
    latency_ms: float
    http_status: int | None
    trace_id: UUID | None
    cached: bool | None = None
    reference_level: RiskLevel | None
    expected_error: GatewayErrorCode | None
    error: GatewayErrorCode | None
    unavailable_reason: UnavailableReason | None = None
    fault: GatewayFault | None
    prediction: Prediction | None
    issues: tuple[Issue, ...]


async def run_cases(
    gateway: Gateway,
    cases: tuple[EvalCase, ...],
    prompt_version: str,
    concurrency: int,
    repeats: int,
) -> tuple[Observation, ...]:
    limit = asyncio.Semaphore(concurrency)

    async def run(case: EvalCase, repetition: int) -> Observation:
        async with limit:
            exchange = await gateway.infer(case, prompt_version)
        return evaluate(case, repetition, exchange, prompt_version)

    observations: list[Observation] = []
    for repetition in range(1, repeats + 1):
        observations.extend(await asyncio.gather(*(run(case, repetition) for case in cases)))
    return tuple(observations)


def evaluate(case: EvalCase, repeat: int, exchange: Exchange, prompt_version: str) -> Observation:
    issues: list[Issue] = []
    response = exchange.response
    prediction = None
    if exchange.fault is not None:
        issues.append(exchange.fault)
    elif isinstance(response, GatewayError):
        if not isinstance(case.expected, ErrorExpectation):
            issues.append("unexpected_error")
        elif response.code != case.expected.code or exchange.status != case.expected.status:
            issues.append("wrong_error")
    elif isinstance(response, GatewaySuccess):
        if isinstance(case.expected, ErrorExpectation):
            issues.append("unexpected_success")
        else:
            prediction = _check_prediction(case, case.expected, response, prompt_version, issues)
    else:
        issues.append("invalid_gateway_response")

    trace_id = response.trace_id if response is not None else None
    if exchange.status is not None and trace_id is None:
        issues.append("missing_trace_id")
    return Observation(
        case_id=case.case_id,
        stratum=case.stratum,
        repeat=repeat,
        latency_ms=exchange.latency_ms,
        http_status=exchange.status,
        trace_id=trace_id,
        cached=response.cached if isinstance(response, GatewaySuccess) else None,
        reference_level=case.expected.level if isinstance(case.expected, RiskExpectation) else None,
        expected_error=case.expected.code if isinstance(case.expected, ErrorExpectation) else None,
        error=response.code if isinstance(response, GatewayError) else None,
        unavailable_reason=exchange.unavailable_reason,
        fault=exchange.fault,
        prediction=prediction,
        issues=tuple(issues),
    )


def _check_prediction(
    case: EvalCase,
    expected: RiskExpectation,
    response: GatewaySuccess,
    prompt_version: str,
    issues: list[Issue],
) -> Prediction:
    texts, rules, penalty = case.risk_context()
    answer = response.answer
    if response.job_id != case.case_id:
        issues.append("wrong_job")
    if response.prompt_version != prompt_version:
        issues.append("wrong_prompt_version")
    if (
        answer.rules_version != rules.rules_version
        or answer.penalty_daily_threshold_percent != rules.penalty_daily_threshold_percent
    ):
        issues.append("wrong_rules")
    if answer.penalty_daily_percent != penalty.daily_percent:
        issues.append("wrong_penalty")
    if answer.threshold_exceeded != expected.threshold_exceeded:
        issues.append("wrong_threshold_result")
    if expected.conflict is not None and answer.conflict != expected.conflict:
        issues.append("wrong_conflict")
    if "human_reason" in expected.model_fields_set and answer.human_reason != expected.human_reason:
        issues.append("wrong_human_reason")
    if expected.confidence is not None and response.confidence != expected.confidence:
        issues.append("wrong_confidence")

    sources = tuple(dict.fromkeys(answer.hypothesis.sources))
    if answer.source_ids != sources or any(source > len(texts) for source in sources):
        issues.append("invalid_sources")
    if not set(expected.source_ids).issubset(answer.source_ids):
        issues.append("missing_expected_source")
    return Prediction(
        level=answer.hypothesis.risk_level,
        model_confidence=answer.hypothesis.confidence,
        effective_confidence=response.confidence,
        model_version=response.model_version,
        prompt_version=response.prompt_version,
        rules_version=answer.rules_version,
        threshold_exceeded=answer.threshold_exceeded,
        conflict=answer.conflict,
        human_reason=answer.human_reason,
        source_ids=answer.source_ids,
    )
