import json

import httpx
import pytest

from tender_kb.dependencies import EVAL_HEADER, GatewayEmbeddings, HttpGate
from tender_kb.domain import DependencyUnavailable, EvalRequest


async def test_gate_preserves_version_and_baseline_and_validates_boolean() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/kb/check"
        assert request.headers[EVAL_HEADER] == "local-unit-test-eval"
        assert json.loads(request.content) == {
            "candidate_version": "kb-demo-2",
            "baseline_version": "kb-demo-1",
            "test_set_version": "kb-regression-v1",
            "test_cases": [],
        }
        return httpx.Response(200, json={"passed": "true", "report": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gate = HttpGate(client, "http://th-eval", "local-unit-test-eval", 3)
        with pytest.raises(DependencyUnavailable):
            await gate.check(
                EvalRequest(
                    candidate_version="kb-demo-2",
                    baseline_version="kb-demo-1",
                    test_set_version="kb-regression-v1",
                    test_cases=[],
                )
            )


async def test_gateway_uses_versioned_gateway_contract_and_batches_texts() -> None:
    sizes: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/invoke"
        body = json.loads(request.content)
        assert body["task"] == "embeddings"
        assert body["mask"] is True
        assert body["prompt_version"] == "v1"
        sizes.append(len(body["context"]))
        return httpx.Response(
            200,
            json={
                "job_id": body["job_id"],
                "trace_id": body["trace_id"],
                "answer": {
                    "kind": "embeddings",
                    "vectors": [[0.0, 1.0] for _ in body["context"]],
                    "source_ids": list(range(1, sizes[-1] + 1)),
                },
                "confidence": 1.0,
                "model_version": "local-model",
                "prompt_version": "v1",
                "masked": True,
                "cached": False,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GatewayEmbeddings(client, "http://modelgw", "v1", 2)
        assert await provider.encode(["public procurement"] * 13) == [[0.0, 1.0]] * 13
        assert sizes == [12, 1]
        with pytest.raises(DependencyUnavailable):
            await GatewayEmbeddings(client, "http://modelgw", "v1", 3).encode(["x"])
