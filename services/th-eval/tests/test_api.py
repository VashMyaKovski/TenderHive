import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from tender_eval.api import create_app
from tender_eval.application import compare_saved_runs, evaluate_run
from tender_eval.cases import Dataset, DatasetError, load_dataset
from tender_eval.config import ApiSettings
from tender_eval.run_models import RunRequest
from tender_eval.storage import Store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings() -> ApiSettings:
    return ApiSettings.model_validate(
        {
            "dataset_path": ROOT / "datasets" / "risk-v1.jsonl",
            "fixture_path": ROOT / "fixtures" / "risk-v1.jsonl",
            "report_dir": ROOT / "reports",
            "gateway_url": "http://stub.invalid",
            "prompt_version": "v1",
            "model_label": "fixture/simulated-v1",
            "timeout_seconds": 2,
            "concurrency": 2,
            "repeats": 1,
            "max_attempts": 40,
            "ece_buckets": 10,
            "min_risk_macro_f1": None,
            "otlp_metrics_url": None,
            "otlp_timeout_seconds": 1,
            "postgres_dsn": "postgresql://th_eval:local_test_only@127.0.0.1:5432/th_eval",
            "database_timeout_seconds": 5,
            "bind_host": "127.0.0.1",
            "port": 8000,
            "api_mode": "fixture",
            "kb_version": "kb-eval-v1",
        }
    )


def new_run(settings: ApiSettings) -> RunRequest:
    return RunRequest(
        run_id=uuid4(),
        suite="risk-v1",
        kb_version=settings.kb_version,
        model_version="fixture/simulated-v1",
        prompt_version=settings.prompt_version,
        targets=("th-docintel", "th-modelgw"),
    )


def test_database_settings_are_required_and_dsn_is_redacted(
    settings: ApiSettings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TH_EVAL_POSTGRES_DSN", raising=False)
    with pytest.raises(ValidationError, match="postgres_dsn"):
        ApiSettings.model_validate({})
    values = {**settings.model_dump(), "postgres_dsn": "sensitive-invalid-dsn"}
    with pytest.raises(ValidationError) as error:
        ApiSettings.model_validate(values)
    assert "sensitive-invalid-dsn" not in str(error.value)


async def test_fixture_results_have_explicit_provenance_and_missing_metrics(
    settings: ApiSettings,
) -> None:
    request = new_run(settings)
    dataset = load_dataset(settings.dataset_path)
    result, observations = await evaluate_run(settings, dataset, request, "fixture")

    assert result.run_id == request.run_id
    assert result.mode == "fixture"
    assert result.targets["th-docintel"].reason == "no_adapter"
    assert result.targets["th-modelgw"].status == "completed"
    assert (
        result.metrics.precision
        is result.metrics.recall
        is result.metrics.trace_completeness
        is None
    )
    assert result.metrics.ece is not None and result.metrics.p95_latency_ms is not None
    assert result.metrics.trace_id_coverage == 1
    assert result.metrics.repeatability is None
    assert len(observations) == 16 and result.regressions == ()
    assert result.gateway is not None
    assert result.gateway.aggregate.gateway_cache_hits == 0
    assert result.gateway.aggregate.gateway_cache_observations == 9


async def test_evaluation_without_an_adapter_has_no_observations(settings: ApiSettings) -> None:
    request = new_run(settings).model_copy(update={"targets": ("th-solver", "th-orchestrator")})
    result, observations = await evaluate_run(
        settings, load_dataset(settings.dataset_path), request, "fixture"
    )
    assert all(target.reason == "no_adapter" for target in result.targets.values())
    assert all(value is None for value in result.metrics.model_dump().values())
    assert not (observations or result.curves or result.by_group or result.regressions)


async def test_error_envelopes_use_safe_messages_and_correlated_trace_ids(
    settings: ApiSettings,
) -> None:
    store = Store(settings.postgres_dsn.get_secret_value(), settings.database_timeout_seconds)
    app = create_app(settings, store)
    trace_id = str(uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        bad_input = await client.post(
            "/api/v1/runs",
            json={"model_version": "sensitive input"},
            headers={"X-Trace-Id": trace_id},
        )
        assert bad_input.status_code == 422
        assert bad_input.headers["X-Trace-Id"] == trace_id
        assert bad_input.json() == {
            "code": "invalid_request",
            "message": "Invalid evaluation request.",
            "trace_id": trace_id,
        }
        assert "sensitive input" not in bad_input.text
        unknown = await client.get("/api/v1/unknown", headers={"X-Trace-Id": "invalid"})
        assert unknown.status_code == 404
        assert unknown.json()["code"] == "not_found"
        assert unknown.json()["trace_id"] == str(UUID(unknown.headers["X-Trace-Id"]))


async def test_knowledge_comparison_requires_matching_live_measurements(
    settings: ApiSettings,
) -> None:
    dataset = load_dataset(settings.dataset_path)
    request = new_run(settings)
    result, _ = await evaluate_run(settings, dataset, request, "fixture")
    assert result.gateway is not None
    baseline_report = result.gateway.model_copy(
        update={
            "run": result.gateway.run.model_copy(update={"mode": "live", "fixture_sha256": None}),
        }
    )
    baseline = result.model_copy(update={"mode": "live", "gateway": baseline_report})
    candidate_version = "kb-candidate"
    candidate_cases = tuple(
        case.model_copy(
            update={
                "context": tuple(
                    {**item, "rules_version": candidate_version}
                    if isinstance(item, dict) and item.get("type") == "knowledge"
                    else item
                    for item in case.context
                )
            }
        )
        for case in dataset.cases
    )
    candidate_dataset = Dataset(dataset.version, "different-version-digest", candidate_cases)
    candidate_id = uuid4()
    candidate_report = baseline_report.model_copy(
        update={
            "run": baseline_report.run.model_copy(
                update={"run_id": candidate_id, "dataset_sha256": candidate_dataset.digest}
            ),
            "observed_rules": (candidate_version,),
        }
    )
    candidate = baseline.model_copy(
        update={
            "run_id": candidate_id,
            "kb_version": candidate_version,
            "gateway": candidate_report,
        }
    )
    result = compare_saved_runs(baseline, candidate, dataset, candidate_dataset)
    assert result["candidate_kb_version"] == candidate_version
    with pytest.raises(DatasetError, match="matching inputs"):
        compare_saved_runs(
            baseline, candidate, dataset, Dataset(dataset.version, dataset.digest, ())
        )
    with pytest.raises(DatasetError, match="verified model"):
        compare_saved_runs(
            baseline,
            candidate.model_copy(update={"model_version": "different"}),
            dataset,
            candidate_dataset,
        )


def test_postgresql_run_lifecycle(settings: ApiSettings) -> None:
    dsn = os.environ.get("TH_EVAL_TEST_POSTGRES_DSN")
    if dsn is None:
        pytest.skip("set TH_EVAL_TEST_POSTGRES_DSN to an isolated PostgreSQL test database")
    store = Store(dsn, settings.database_timeout_seconds)

    async def scenario() -> None:
        dataset = load_dataset(settings.dataset_path)
        await store.migrate(dataset, settings.kb_version)
        await store.migrate(dataset, settings.kb_version)
        app = create_app(settings, store)
        data = new_run(settings).model_dump(mode="json")
        run_id = data["run_id"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.get(f"/api/v1/runs/{run_id}")).status_code == 404
            assert (
                await client.post("/api/v1/runs", json={**data, "targets": ["unknown"]})
            ).status_code == 422
            assert (
                await client.post("/api/v1/runs", json={**data, "kb_version": "missing"})
            ).status_code == 404
            async with store.claim(RunRequest.model_validate(data).run_id):
                assert (await client.post("/api/v1/runs", json=data)).status_code == 409
            created = await client.post("/api/v1/runs", json=data)
            assert created.status_code == 201, created.text
            assert created.json()["regressions"] == []
            retry = await client.post("/api/v1/runs", json=data)
            assert retry.status_code == 200 and retry.json() == created.json()
            assert (
                await client.post("/api/v1/runs", json={**data, "model_version": "different"})
            ).status_code == 409
            candidate = await client.post("/api/v1/runs", json={**data, "run_id": str(uuid4())})
            assert candidate.status_code == 201
            response = await client.get(
                f"/api/v1/runs/{candidate.json()['run_id']}/comparison",
                params={"baseline_run_id": run_id},
            )
            assert response.status_code == 200
            assert response.json()["delta"]["risk_macro_f1"] == 0
        restart = create_app(settings, Store(dsn, settings.database_timeout_seconds))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(restart), base_url="http://test"
        ) as client:
            assert (await client.get(f"/api/v1/runs/{run_id}")).json() == created.json()

    asyncio.run(
        scenario(), loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None
    )
