import csv
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, ValidationError

from tender_eval.cases import Contract, DatasetError, Stratum
from tender_eval.metrics import Summary, summarize
from tender_eval.runner import Observation

STRATA: tuple[Stratum, ...] = ("typical", "boundary", "negative")
type Mode = Literal["fixture", "live"]
type CacheProfile = Literal["not_applicable", "unverified", "isolated_cold", "warm"]


class RunManifest(Contract):
    run_id: UUID
    started_at: AwareDatetime
    mode: Mode
    dataset_version: str
    dataset_sha256: str
    fixture_sha256: str | None
    case_ids: tuple[UUID, ...]
    model_label: str
    prompt_version: str
    cache_profile: CacheProfile
    cache_bypass_verified: Literal[False] = False
    repeats: int
    concurrency: int
    timeout_seconds: float
    ece_buckets: int
    min_risk_macro_f1: float | None


class MetricReport(Contract):
    schema_version: Literal[1] = 1
    run: RunManifest
    observed_models: tuple[str, ...]
    observed_prompts: tuple[str, ...]
    observed_rules: tuple[str, ...]
    complete_trace_coverage: Literal[None] = None
    requirement_extraction_metrics: Literal[None] = None
    aggregate: Summary
    strata: dict[Stratum, Summary]
    quality_gate_passed: bool | None

    @property
    def passed(self) -> bool:
        return self.aggregate.failed == 0 and self.quality_gate_passed is not False


def build_report(run: RunManifest, observations: tuple[Observation, ...]) -> MetricReport:
    aggregate = summarize(observations, run.ece_buckets)
    strata: dict[Stratum, Summary] = {}
    for stratum in STRATA:
        subset = tuple(item for item in observations if item.stratum == stratum)
        if subset:
            strata[stratum] = summarize(subset, run.ece_buckets)
    predicted = [item.prediction for item in observations if item.prediction is not None]
    gate = None
    if run.min_risk_macro_f1 is not None:
        gate = (
            aggregate.risk_macro_f1 is not None and aggregate.risk_macro_f1 >= run.min_risk_macro_f1
        )
    return MetricReport(
        run=run,
        observed_models=tuple(sorted({item.model_version for item in predicted})),
        observed_prompts=tuple(sorted({item.prompt_version for item in predicted})),
        observed_rules=tuple(sorted({item.rules_version for item in predicted})),
        aggregate=aggregate,
        strata=strata,
        quality_gate_passed=gate,
    )


def write_report(root: Path, report: MetricReport, observations: tuple[Observation, ...]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    destination = root / str(report.run.run_id)
    with TemporaryDirectory(dir=root) as temporary:
        staging = Path(temporary)
        (staging / "report.json").write_text(
            report.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        with (staging / "cases.jsonl").open("w", encoding="utf-8") as stream:
            for item in observations:
                stream.write(item.model_dump_json() + "\n")
        _write_curves(staging / "curves.csv", report)
        staging.rename(destination)
    return destination


def _write_curves(path: Path, report: MetricReport) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            ("stratum", "curve", "x", "accuracy", "samples", "bin_lower", "bin_upper", "threshold")
        )
        for stratum, metrics in (("all", report.aggregate), *report.strata.items()):
            for point in metrics.calibration:
                writer.writerow(
                    (
                        stratum,
                        "calibration",
                        point.confidence,
                        point.accuracy,
                        point.count,
                        point.lower,
                        point.upper,
                        None,
                    )
                )
            for point in metrics.quality_curve:
                writer.writerow(
                    (
                        stratum,
                        "quality_coverage",
                        point.coverage,
                        point.accuracy,
                        point.retained,
                        None,
                        None,
                        point.minimum_confidence,
                    )
                )


def read_report(path: Path) -> MetricReport:
    try:
        return MetricReport.model_validate_json(path.read_bytes())
    except OSError, ValidationError:
        raise DatasetError("cannot read a valid version 1 evaluation report") from None


def _measurement_settings(report: MetricReport) -> tuple[object, ...]:
    run = report.run
    return (
        run.mode,
        run.case_ids,
        run.cache_profile,
        run.repeats,
        run.concurrency,
        run.timeout_seconds,
        run.ece_buckets,
    )


def compare_reports(baseline: MetricReport, candidate: MetricReport) -> dict[str, object]:
    if (
        _measurement_settings(baseline) != _measurement_settings(candidate)
        or baseline.run.dataset_sha256 != candidate.run.dataset_sha256
        or baseline.run.fixture_sha256 != candidate.run.fixture_sha256
        or baseline.observed_rules != candidate.observed_rules
    ):
        raise DatasetError("reports must use equal data, rules, mode and measurement settings")
    return _report_delta(baseline, candidate)


def compare_knowledge_reports(baseline: MetricReport, candidate: MetricReport) -> dict[str, object]:
    if (
        baseline.run.mode != "live"
        or _measurement_settings(baseline) != _measurement_settings(candidate)
        or baseline.observed_models != candidate.observed_models
        or baseline.observed_prompts != candidate.observed_prompts
        or baseline.run.prompt_version != candidate.run.prompt_version
    ):
        raise DatasetError("knowledge comparison requires live runs with matching model and prompt")
    return _report_delta(baseline, candidate)


def _report_delta(baseline: MetricReport, candidate: MetricReport) -> dict[str, object]:
    if len(baseline.observed_models) != 1 or len(candidate.observed_models) != 1:
        raise DatasetError("comparison requires one observed model per run")
    before, after = baseline.aggregate, candidate.aggregate
    pairs = {
        "risk_macro_f1": (before.risk_macro_f1, after.risk_macro_f1),
        "ece": (before.ece, after.ece),
        "p95_latency_ms": (before.p95_latency_ms, after.p95_latency_ms),
        "trace_id_coverage": (before.trace_id_coverage, after.trace_id_coverage),
        "repeat_agreement": (before.repeat_agreement, after.repeat_agreement),
    }
    return {
        "baseline": {
            "run_id": str(baseline.run.run_id),
            "model": baseline.observed_models[0],
            "prompt": baseline.run.prompt_version,
        },
        "candidate": {
            "run_id": str(candidate.run.run_id),
            "model": candidate.observed_models[0],
            "prompt": candidate.run.prompt_version,
        },
        "mode": candidate.run.mode,
        "delta": {
            name: new - old if old is not None and new is not None else None
            for name, (old, new) in pairs.items()
        },
    }
