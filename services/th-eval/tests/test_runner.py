import asyncio
from pathlib import Path

import httpx
import pytest

from tender_eval.cases import DatasetError, load_dataset
from tender_eval.fixtures import load_fixtures
from tender_eval.gateway import HttpGateway
from tender_eval.runner import run_cases

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "datasets" / "risk-v1.jsonl"
FIXTURES = ROOT / "fixtures" / "risk-v1.jsonl"


async def test_reference_run_through_http_transport() -> None:
    dataset = load_dataset(DATASET)
    fixtures = load_fixtures(FIXTURES, dataset.version, dataset.cases)
    async with httpx.AsyncClient(
        base_url="http://stub.invalid", transport=fixtures.make_transport("v1")
    ) as client:
        observations = await run_cases(HttpGateway(client, 1), dataset.cases, "v1", 3, 2)

    assert len(observations) == len(dataset.cases) * 2
    assert not any(result.issues for result in observations)
    assert len({result.trace_id for result in observations}) == len(observations)
    assert observations[6].prediction is not None
    assert observations[6].prediction.effective_confidence == 0
    assert observations[13].error == "provider_timeout"
    assert observations[13].fault is None


async def test_returns_timeout_and_rejects_error_with_cached_answer() -> None:
    dataset = load_dataset(DATASET)

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(dataset.cases[0].case_id).encode() in request.content:
            await asyncio.sleep(1)
        return httpx.Response(502, json={"code": "provider_rejected", "answer": {"cached": True}})

    async with httpx.AsyncClient(
        base_url="http://stub.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        slow, cached = await run_cases(
            HttpGateway(client, 0.05), (dataset.cases[0], dataset.cases[12]), "v1", 2, 1
        )

    assert slow.issues == ("transport_timeout",)
    assert slow.http_status is None
    assert cached.issues == ("invalid_gateway_response", "missing_trace_id")
    assert cached.http_status == 502
    assert cached.prediction is None


def test_fixtures_match_dataset(tmp_path: Path) -> None:
    dataset = load_dataset(DATASET)
    path = tmp_path / "incomplete.jsonl"
    header, first, *_ = FIXTURES.read_text(encoding="utf-8").splitlines()
    path.write_text(f"{header}\n{first}\n", encoding="utf-8")

    with pytest.raises(DatasetError, match="must match the dataset case IDs"):
        load_fixtures(path, dataset.version, dataset.cases)
