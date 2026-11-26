from typing import Literal

from tender_eval.cases import Dataset, DatasetError, RiskExpectation, Stratum
from tender_eval.config import Settings
from tender_eval.execution import execute
from tender_eval.metrics import Summary
from tender_eval.report import Mode, compare_knowledge_reports, compare_reports
from tender_eval.run_models import (
    Curves,
    Regression,
    RunMetrics,
    RunRequest,
    RunResult,
    Target,
    TargetResult,
)
from tender_eval.runner import Observation


def _metrics(summary: Summary) -> RunMetrics:
    return RunMetrics(
        ece=summary.ece,
        p95_latency_ms=summary.p95_latency_ms,
        trace_id_coverage=summary.trace_id_coverage,
        repeatability=summary.repeat_agreement,
        escalation_rate=summary.escalation_rate,
    )


async def evaluate_run(
    settings: Settings, dataset: Dataset, request: RunRequest, mode: Mode
) -> tuple[RunResult, tuple[Observation, ...]]:
    targets: dict[Target, TargetResult] = {
        target: TargetResult(status="unavailable", reason="no_adapter")
        for target in request.targets
    }
    gateway = None
    metrics = RunMetrics()
    by_group: dict[Stratum, RunMetrics] = {}
    regressions: list[Regression] = []
    curves: list[Curves] = []
    observations = ()
    if "th-modelgw" in targets:
        live = mode == "live"
        gateway, observations = await execute(
            settings,
            dataset,
            request.run_id,
            live,
            "unverified" if live else "not_applicable",
            request.model_version,
            request.prompt_version,
        )
        targets["th-modelgw"] = (
            TargetResult(status="completed", reason=None)
            if any(item.fault is None for item in observations)
            else TargetResult(status="unavailable", reason="no_gateway_response")
        )
        metrics = _metrics(gateway.aggregate)
        by_group = {group: _metrics(summary) for group, summary in gateway.strata.items()}
        groups: tuple[tuple[Stratum | Literal["all"], Summary], ...] = (
            ("all", gateway.aggregate),
            *gateway.strata.items(),
        )
        curves = [
            Curves(
                group=group, calibration=summary.calibration, quality_coverage=summary.quality_curve
            )
            for group, summary in groups
            if summary.ece_samples
        ]
        regressions = [
            Regression(
                case_id=item.case_id, repeat=item.repeat, trace_id=item.trace_id, issues=item.issues
            )
            for item in observations
            if item.issues
        ]
        if live and set(gateway.observed_models) - {request.model_version}:
            regressions.append(
                Regression(
                    case_id=None, repeat=None, trace_id=None, issues=("unexpected_model_version",)
                )
            )
        if gateway.quality_gate_passed is False:
            regressions.append(
                Regression(
                    case_id=None, repeat=None, trace_id=None, issues=("quality_gate_failed",)
                )
            )
    return RunResult(
        run_id=request.run_id,
        suite=request.suite,
        kb_version=request.kb_version,
        model_version=request.model_version,
        prompt_version=request.prompt_version,
        mode=mode,
        targets=targets,
        metrics=metrics,
        by_group=by_group,
        regressions=tuple(regressions),
        curves=tuple(curves),
        gateway=gateway,
    ), observations


def compare_saved_runs(
    baseline: RunResult, candidate: RunResult, baseline_cases: Dataset, candidate_cases: Dataset
) -> dict[str, object]:
    if baseline.gateway is None or candidate.gateway is None:
        raise DatasetError("comparison requires gateway evaluations in both runs")
    if baseline.suite != candidate.suite or baseline.targets.keys() != candidate.targets.keys():
        raise DatasetError("comparison requires the same suite and targets")
    if baseline.kb_version == candidate.kb_version:
        delta = compare_reports(baseline.gateway, candidate.gateway)
    else:
        if _knowledge_comparison_inputs(baseline_cases) != _knowledge_comparison_inputs(
            candidate_cases
        ):
            raise DatasetError("knowledge comparison requires matching inputs and reference labels")
        for run in (baseline, candidate):
            assert run.gateway is not None
            if (
                run.gateway.observed_rules != (run.kb_version,)
                or run.gateway.observed_models != (run.model_version,)
                or run.gateway.observed_prompts != (run.prompt_version,)
            ):
                raise DatasetError(
                    "knowledge comparison requires verified model, prompt and rules versions"
                )
        delta = compare_knowledge_reports(baseline.gateway, candidate.gateway)
    return {
        **delta,
        "baseline_kb_version": baseline.kb_version,
        "candidate_kb_version": candidate.kb_version,
    }


def _knowledge_comparison_inputs(dataset: Dataset) -> tuple[tuple[object, ...], ...]:
    # The supplied knowledge snapshot is the variable. Keep selected cases and quality labels fixed.
    return tuple(
        (
            case.case_id,
            case.stratum,
            case.mask,
            tuple(
                item
                for item in case.context
                if not isinstance(item, dict) or item.get("type") != "knowledge"
            ),
            (case.expected.kind, case.expected.level)
            if isinstance(case.expected, RiskExpectation)
            else (case.expected.kind, case.expected.status, case.expected.code),
        )
        for case in dataset.cases
        if case.live
    )
