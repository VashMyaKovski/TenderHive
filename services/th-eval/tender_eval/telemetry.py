import asyncio
from time import time_ns

import httpx
from pydantic import BaseModel, Field, ValidationError

from tender_eval.report import MetricReport


class _PartialSuccess(BaseModel):
    rejected_data_points: int = Field(default=0, alias="rejectedDataPoints")


class _ExportResponse(BaseModel):
    partial_success: _PartialSuccess | None = Field(default=None, alias="partialSuccess")


def _attribute(key: str, value: str) -> dict[str, object]:
    return {"key": key, "value": {"stringValue": value}}


def metric_payload(report: MetricReport, timestamp_ns: int) -> dict[str, object]:
    series: dict[str, list[dict[str, object]]] = {}
    for stratum, summary in (("all", report.aggregate), *report.strata.items()):
        attributes = [
            _attribute("stratum", stratum),
            _attribute("run_mode", report.run.mode),
            _attribute("cache_profile", report.run.cache_profile),
        ]
        values = {
            "th_eval_risk_macro_f1": summary.risk_macro_f1,
            "th_eval_ece": summary.ece,
            "th_eval_trace_id_coverage": summary.trace_id_coverage,
            "th_eval_case_attempts": summary.attempts,
            "th_eval_case_failures": summary.failed,
            "th_eval_unexpected_errors": summary.unexpected_errors,
            "th_eval_gateway_latency_p50_seconds": (
                summary.p50_latency_ms / 1000 if summary.p50_latency_ms is not None else None
            ),
            "th_eval_gateway_latency_p95_seconds": (
                summary.p95_latency_ms / 1000 if summary.p95_latency_ms is not None else None
            ),
        }
        for name, value in values.items():
            if value is not None:
                series.setdefault(name, []).append(
                    {
                        "attributes": attributes,
                        "timeUnixNano": str(timestamp_ns),
                        "asDouble": value,
                    }
                )

    metrics = [{"name": name, "gauge": {"dataPoints": points}} for name, points in series.items()]
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": [_attribute("service.name", "th-eval")]},
                "scopeMetrics": [{"scope": {"name": "tender_eval"}, "metrics": metrics}],
            }
        ]
    }


async def publish_metrics(
    client: httpx.AsyncClient,
    endpoint: str,
    timeout_seconds: float,
    report: MetricReport,
) -> bool:
    try:
        async with asyncio.timeout(timeout_seconds):
            response = await client.post(endpoint, json=metric_payload(report, time_ns()))
            response.raise_for_status()
            receipt = (
                _ExportResponse.model_validate_json(response.content) if response.content else None
            )
    except httpx.HTTPError, TimeoutError, ValidationError:
        return False
    return not (
        receipt and receipt.partial_success and receipt.partial_success.rejected_data_points
    )
