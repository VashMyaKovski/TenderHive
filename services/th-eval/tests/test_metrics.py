import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from tender_eval.cases import DatasetError, RiskLevel
from tender_eval.metrics import summarize
from tender_eval.report import RunManifest, build_report, compare_reports, read_report, write_report
from tender_eval.runner import Observation, Prediction
from tender_eval.telemetry import publish_metrics


def observation(
    reference: RiskLevel,
    label: RiskLevel | None,
    confidence: float,
    duration: float,
) -> Observation:
    identifier = uuid4()
    prediction = (
        Prediction(
            level=label,
            model_confidence=confidence,
            effective_confidence=confidence,
            model_version="fixture/v1",
            prompt_version="v1",
            rules_version="kb-v1",
            threshold_exceeded=False,
            conflict=False,
            human_reason=None,
            source_ids=(1,),
        )
        if label is not None
        else None
    )
    return Observation(
        case_id=identifier,
        stratum="typical",
        repeat=1,
        latency_ms=duration,
        http_status=200 if prediction else None,
        trace_id=uuid4() if prediction else None,
        reference_level=reference,
        expected_error=None,
        error=None,
        fault=None if prediction else "transport_timeout",
        prediction=prediction,
        issues=() if prediction else ("transport_timeout",),
    )


def manifest(case_ids: tuple[UUID, ...]) -> RunManifest:
    return RunManifest(
        run_id=uuid4(),
        started_at=datetime.now(UTC),
        mode="fixture",
        dataset_version="risk-v1",
        dataset_sha256="dataset",
        fixture_sha256="fixture",
        case_ids=case_ids,
        model_label="fixture",
        prompt_version="v1",
        cache_profile="not_applicable",
        repeats=1,
        concurrency=1,
        timeout_seconds=2,
        ece_buckets=2,
        min_risk_macro_f1=0.9,
    )


def test_metrics_against_hand_computed_values() -> None:
    observations = (
        observation("low", "low", 0.4, 10),
        observation("high", "low", 0.6, 20),
        observation("high", None, 0, 40),
        observation("medium", "medium", 1.0, 30),
    )
    result = summarize(observations, 2)

    assert result.labelled == 4 and result.scored == 3
    assert result.confusion["high"] == {"low": 1, "medium": 0, "high": 0, "missing": 1}
    assert result.classes["low"].precision == 0.5
    assert result.classes["high"].recall == 0
    assert result.risk_macro_f1 == pytest.approx(5 / 9)
    assert result.ece == pytest.approx(0.4)
    assert result.calibration[-1].count == 2  # The last bin includes confidence 1.0.
    assert result.quality_curve[1].coverage == 0.5
    assert result.quality_curve[1].accuracy == 0.5
    assert (result.p50_latency_ms, result.p95_latency_ms) == (20, 40)
    assert result.trace_id_coverage == 1 and result.trace_eligible_responses == 3
    assert result.unexpected_errors == 1


def test_repeat_agreement_ignores_trace_ids_and_timings() -> None:
    first = observation("low", "low", 0.9, 10)
    second = first.model_copy(update={"trace_id": uuid4(), "latency_ms": 20, "repeat": 2})
    assert summarize((first, second), 2).repeat_agreement == 1


def test_repeat_agreement_is_unavailable_without_a_gateway_result() -> None:
    first = observation("low", None, 0.9, 10)
    second = first.model_copy(update={"latency_ms": 20, "repeat": 2})
    summary = summarize((first, second), 2)
    assert summary.repeat_agreement is None
    assert summary.repeated_cases == 0


def test_report_and_compare(tmp_path: Path) -> None:
    result = observation("low", "low", 0.4, 10)
    run = manifest((result.case_id,))
    report = build_report(run, (result,))
    destination = write_report(tmp_path, report, (result,))
    assert read_report(destination / "report.json") == report
    assert (destination / "cases.jsonl").read_text(encoding="utf-8").count("\n") == 1
    assert "quality_coverage" in (destination / "curves.csv").read_text(encoding="utf-8")
    assert report.passed
    assert report.complete_trace_coverage is None
    assert compare_reports(report, report)["delta"] == {
        "risk_macro_f1": 0,
        "ece": 0,
        "p95_latency_ms": 0,
        "trace_id_coverage": 0,
        "repeat_agreement": None,
    }

    incomparable = build_report(run.model_copy(update={"ece_buckets": 3}), (result,))
    with pytest.raises(DatasetError, match="equal data, rules, mode"):
        compare_reports(report, incomparable)


def test_metrics_with_no_observations_are_undefined() -> None:
    result = summarize((), 2)
    assert result.ece is None
    assert result.risk_macro_f1 is None
    assert result.p95_latency_ms is None
    assert result.repeat_agreement is None


async def test_otlp_exports_aggregates_without_run_or_trace_ids() -> None:
    observed = observation("low", "low", 0.8, 25)
    report = build_report(manifest((observed.case_id,)), (observed,))

    def receive(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/metrics"
        raw = request.content.decode()
        assert str(observed.trace_id) not in raw
        assert str(report.run.run_id) not in raw
        assert str(observed.case_id) not in raw
        metrics = json.loads(raw)["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        p95 = next(
            metric for metric in metrics if metric["name"] == "th_eval_gateway_latency_p95_seconds"
        )
        assert p95["gauge"]["dataPoints"][0]["asDouble"] == 0.025
        return httpx.Response(200, json={"partialSuccess": {"rejectedDataPoints": "1"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        assert not await publish_metrics(client, "http://collector.invalid/v1/metrics", 1, report)
