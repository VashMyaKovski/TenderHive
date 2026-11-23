from collections import defaultdict
from math import ceil
from uuid import UUID

from tender_eval.cases import Contract, RiskLevel
from tender_eval.runner import Observation

RISK_LEVELS: tuple[RiskLevel, ...] = ("low", "medium", "high")


class ClassMetrics(Contract):
    support: int
    predicted: int
    precision: float | None
    recall: float | None
    f1: float | None


class CalibrationBin(Contract):
    lower: float
    upper: float
    count: int
    confidence: float | None
    accuracy: float | None


class QualityPoint(Contract):
    minimum_confidence: float
    retained: int
    coverage: float
    accuracy: float | None


class Summary(Contract):
    attempts: int
    passed: int
    failed: int
    expected_errors_matched: int
    unexpected_errors: int
    escalation_rate: float | None
    labelled: int
    scored: int
    confusion: dict[str, dict[str, int]]
    classes: dict[str, ClassMetrics]
    risk_macro_f1: float | None
    ece: float | None
    ece_samples: int
    calibration: tuple[CalibrationBin, ...]
    quality_curve: tuple[QualityPoint, ...]
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    trace_id_coverage: float | None
    trace_eligible_responses: int
    gateway_cache_hits: int = 0
    gateway_cache_observations: int = 0
    repeat_agreement: float | None
    repeated_cases: int


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def summarize(observations: tuple[Observation, ...], buckets: int) -> Summary:
    labelled = [item for item in observations if item.reference_level is not None]
    scores = [
        (item.prediction.model_confidence, item.prediction.level == item.reference_level)
        for item in labelled
        if item.prediction is not None
    ]
    confusion: dict[str, dict[str, int]] = {
        reference: dict.fromkeys((*RISK_LEVELS, "missing"), 0) for reference in RISK_LEVELS
    }
    for item in labelled:
        assert item.reference_level is not None
        label = item.prediction.level if item.prediction is not None else "missing"
        confusion[item.reference_level][label] += 1
    classes = {level: _class_metrics(level, confusion) for level in RISK_LEVELS}
    f1_values = [
        metric.f1 for metric in classes.values() if metric.support and metric.f1 is not None
    ]
    calibration, ece = _calibration(scores, buckets)
    quality = (
        tuple(
            _quality_point(index / buckets, scores, len(labelled)) for index in range(buckets + 1)
        )
        if labelled
        else ()
    )
    latencies = sorted(item.latency_ms for item in observations)
    responses = [item for item in observations if item.http_status is not None]
    predictions = [item.prediction for item in observations if item.prediction is not None]
    passed = sum(not item.issues for item in observations)
    agreement, repeated_cases = _repeat_agreement(observations)
    return Summary(
        attempts=len(observations),
        passed=passed,
        failed=len(observations) - passed,
        expected_errors_matched=sum(
            item.expected_error is not None and not item.issues for item in observations
        ),
        unexpected_errors=sum(
            item.fault is not None or (item.error is not None and bool(item.issues))
            for item in observations
        ),
        escalation_rate=_ratio(
            sum(item.human_reason is not None for item in predictions), len(predictions)
        ),
        labelled=len(labelled),
        scored=len(scores),
        confusion=confusion,
        classes=classes,
        risk_macro_f1=sum(f1_values) / len(f1_values) if f1_values else None,
        ece=ece,
        ece_samples=len(scores),
        calibration=calibration,
        quality_curve=quality,
        p50_latency_ms=_percentile(latencies, 0.5),
        p95_latency_ms=_percentile(latencies, 0.95),
        trace_id_coverage=_ratio(
            sum(item.trace_id is not None for item in responses), len(responses)
        ),
        trace_eligible_responses=len(responses),
        gateway_cache_hits=sum(item.cached is True for item in observations),
        gateway_cache_observations=sum(item.cached is not None for item in observations),
        repeat_agreement=agreement,
        repeated_cases=repeated_cases,
    )


def _class_metrics(level: RiskLevel, confusion: dict[str, dict[str, int]]) -> ClassMetrics:
    true_positive = confusion[level][level]
    predicted = sum(row[level] for row in confusion.values())
    support = sum(confusion[level].values())
    return ClassMetrics(
        support=support,
        predicted=predicted,
        precision=_ratio(true_positive, predicted),
        recall=_ratio(true_positive, support),
        f1=_ratio(2 * true_positive, predicted + support),
    )


def _calibration(
    scores: list[tuple[float, bool]], buckets: int
) -> tuple[tuple[CalibrationBin, ...], float | None]:
    grouped: list[list[tuple[float, bool]]] = [[] for _ in range(buckets)]
    for confidence, correct in scores:
        grouped[min(int(confidence * buckets), buckets - 1)].append((confidence, correct))
    bins: list[CalibrationBin] = []
    error = 0.0
    for index, values in enumerate(grouped):
        count = len(values)
        confidence = sum(score for score, _ in values) / count if count else None
        accuracy = _ratio(sum(correct for _, correct in values), count)
        if confidence is not None and accuracy is not None:
            error += count * abs(accuracy - confidence)
        bins.append(
            CalibrationBin(
                lower=index / buckets,
                upper=(index + 1) / buckets,
                count=count,
                confidence=confidence,
                accuracy=accuracy,
            )
        )
    return tuple(bins), error / len(scores) if scores else None


def _quality_point(threshold: float, scores: list[tuple[float, bool]], count: int) -> QualityPoint:
    kept = [correct for confidence, correct in scores if confidence >= threshold]
    return QualityPoint(
        minimum_confidence=threshold,
        retained=len(kept),
        coverage=len(kept) / count,
        accuracy=_ratio(sum(kept), len(kept)),
    )


def _percentile(sorted_values: list[float], percentile: float) -> float | None:
    return sorted_values[ceil(len(sorted_values) * percentile) - 1] if sorted_values else None


def _repeat_agreement(observations: tuple[Observation, ...]) -> tuple[float | None, int]:
    by_case: dict[UUID, list[tuple[object, ...]]] = defaultdict(list)
    observable_cases: set[UUID] = set()
    for item in observations:
        if item.fault is None and item.http_status is not None:
            observable_cases.add(item.case_id)
        predicted = item.prediction
        semantic = (
            (
                predicted.level,
                predicted.human_reason,
                predicted.source_ids,
                predicted.threshold_exceeded,
                predicted.conflict,
                predicted.model_version,
            )
            if predicted is not None
            else ()
        )
        by_case[item.case_id].append(
            (
                item.http_status,
                item.error,
                item.unavailable_reason,
                item.fault,
                item.issues,
                semantic,
            )
        )
    repeated = [set(by_case[case_id]) for case_id in observable_cases if len(by_case[case_id]) > 1]
    return _ratio(sum(len(signatures) == 1 for signatures in repeated), len(repeated)), len(
        repeated
    )
