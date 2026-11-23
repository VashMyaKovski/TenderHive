import asyncio
import json
from hashlib import sha256
from typing import Any
from uuid import uuid4

import httpx
import pytest
from neo4j import AsyncGraphDatabase
from pydantic_settings import BaseSettings
from redis.asyncio import Redis

from tender_kb.api import create_app
from tender_kb.application import Knowledge
from tender_kb.config import Settings
from tender_kb.dependencies import EVAL_HEADER, FixtureEmbeddings, HttpGate, RedisContextCache
from tender_kb.domain import EvalRequest, canonical_json
from tender_kb.postgres import Postgres
from tender_kb.projections import Neo4jGraph, QdrantCorpus


class IntegrationSettings(BaseSettings):
    kb_run_integration: bool = False


def _body(version: str, text: str) -> dict[str, Any]:
    payload = {
        "version": version,
        "rules": [
            {
                "rule_id": "R-DEMO-1",
                "version": 1,
                "condition": {"field": "verified", "op": "eq", "value": True},
                "consequence": {"status": "MET", "reason": "Registry confirms the fact."},
                "priority": 50,
                "scope": {"kind": "license", "regime": "public", "task_class": "compliance"},
            }
        ],
        "facts": [
            {
                "fact_id": "fact-demo",
                "key": "verified",
                "value": True,
                "source": "Synthetic registry",
                "valid_from": "2026-11-21",
                "valid_until": "2026-11-30",
            }
        ],
        "reference_data": {"regime": "public"},
        "graph_slice": {
            "nodes": [
                {"node_id": "lot-1", "kind": "lot"},
                {"node_id": "lot-2", "kind": "lot"},
                {"node_id": "risk-1", "kind": "risk", "lot_id": "lot-1"},
                {"node_id": "resource-1", "kind": "resource"},
            ],
            "edges": [{"source": "risk-1", "target": "resource-1", "relation": "mitigated_by"}],
        },
        "corpus_fragments": [
            {
                "fragment_id": "fragment-1",
                "text": text,
                "source": "Public fixture",
                "date": "2026-11-21",
                "lot_ids": ["lot-1"],
            },
            {
                "fragment_id": "fragment-2",
                "text": "Separate unrelated lot",
                "source": "Public fixture",
                "date": "2026-11-21",
                "lot_ids": ["lot-2"],
            },
        ],
        "test_cases": [{"case_id": "license", "input": {"kind": "license"}, "expected": {}}],
    }
    return {**payload, "checksum": sha256(canonical_json(payload)).hexdigest()}


async def _request_activation(client: httpx.AsyncClient, version: str) -> httpx.Response:
    return await client.post(f"/api/v1/versions/{version}/activate")


async def test_version_lifecycle_and_isolation_with_real_stores() -> None:
    if not IntegrationSettings().kb_run_integration:
        pytest.skip("Set KB_RUN_INTEGRATION=true for disposable local infrastructure.")
    settings = Settings.model_validate({})
    token = settings.eval_token.get_secret_value()
    suffix = uuid4().hex[:12]
    first, second, rejected, racing, invalid = [
        f"kb-integration-{suffix}-{label}"
        for label in ("first", "second", "rejected", "racing", "invalid")
    ]
    seen: list[EvalRequest] = []
    called = asyncio.Event()
    resume = asyncio.Event()
    fail_once = True

    async def run_gate(request: httpx.Request) -> httpx.Response:
        nonlocal fail_once
        assert request.headers[EVAL_HEADER] == token
        assert request.url.path == "/api/v1/kb/check"
        body = EvalRequest.model_validate(json.loads(request.content))
        seen.append(body)
        query = {"version": body.candidate_version}
        assert (await api.get("/api/v1/context", params=query)).status_code == 404
        candidate = await api.get("/api/v1/context", params=query, headers={EVAL_HEADER: token})
        candidate.raise_for_status()
        assert candidate.json()["version"] == body.candidate_version
        assert body.test_cases[0].case_id == "license"
        if body.candidate_version == second and fail_once:
            fail_once = False
            return httpx.Response(503)
        if body.candidate_version == racing and not resume.is_set():
            called.set()
            await resume.wait()
        return httpx.Response(
            200, json={"passed": body.candidate_version != rejected, "report": []}
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(run_gate)) as eval_client,
        httpx.AsyncClient(timeout=settings.io_timeout_seconds, trust_env=False) as store_client,
        AsyncGraphDatabase.driver(
            str(settings.neo4j_uri),
            auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
        ) as driver,
    ):
        postgres = Postgres(str(settings.postgres_dsn), settings.database_timeout_seconds)
        graph = Neo4jGraph(driver, settings.neo4j_database, settings.io_timeout_seconds)
        corpus = QdrantCorpus(
            store_client,
            str(settings.qdrant_url),
            settings.qdrant_collection,
            None,
            FixtureEmbeddings(settings.vector_size),
            settings.vector_size,
        )
        assert settings.redis_url is not None
        async with Redis.from_url(str(settings.redis_url)) as redis:
            knowledge = Knowledge(
                postgres,
                graph,
                corpus,
                HttpGate(eval_client, str(settings.eval_url), token, settings.eval_timeout_seconds),
                RedisContextCache(redis, settings.cache_ttl_seconds),
                settings.test_set_version,
                settings.max_fragments,
            )
            app = create_app(knowledge, token)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://th-kb"
            ) as api:
                assert (await api.get("/healthz")).json() == {"status": "ok"}
                body = _body(first, "License compliance and safety")
                bad_checksum = {**body, "checksum": "0" * 64}
                invalid_upload = await api.post("/api/v1/versions", json=bad_checksum)
                assert invalid_upload.status_code == 422
                assert invalid_upload.json()["code"] == "CHECKSUM_MISMATCH"
                upload = await api.post("/api/v1/versions", json=body)
                assert upload.status_code == 201
                assert upload.json()["status"] == "DRAFT"
                assert (await api.post("/api/v1/versions", json=body)).json() == upload.json()
                changed = await api.post("/api/v1/versions", json=_body(first, "Changed"))
                assert changed.status_code == 409
                assert (
                    await api.get("/api/v1/context", params={"version": first})
                ).status_code == 404
                assert (await _request_activation(api, first)).json()["status"] == "ACTIVE"
                params = {"lot_ids": "lot-1", "query": "license", "as_of": "2026-11-23"}
                response = await api.get("/api/v1/context", params=params)
                response.raise_for_status()
                first_context = response.json()
                assert first_context["version"] == first
                assert [node["node_id"] for node in first_context["graph_slice"]["nodes"]] == [
                    "lot-1",
                    "resource-1",
                    "risk-1",
                ]
                assert first_context["graph_slice"]["edges"][0]["relation"] == "mitigated_by"
                assert [frag["text"] for frag in first_context["corpus_fragments"]] == [
                    "License compliance and safety"
                ]
                assert first_context["facts"][0]["key"] == "verified"
                expired = (await api.get("/api/v1/context", params={"as_of": "2026-12-01"})).json()
                assert expired["facts"] == []

                for version, text in (
                    (second, "License catalogue for contractors"),
                    (rejected, "License failed"),
                    (racing, "License new candidate"),
                ):
                    result = await api.post("/api/v1/versions", json=_body(version, text))
                    assert result.status_code == 201

                invalid_body = _body(invalid, "Invalid graph")
                invalid_body["graph_slice"]["edges"][0]["target"] = "not-found"
                del invalid_body["checksum"]
                invalid_body["checksum"] = sha256(canonical_json(invalid_body)).hexdigest()
                assert (await api.post("/api/v1/versions", json=invalid_body)).status_code == 201
                invalid_activation = await _request_activation(api, invalid)
                assert invalid_activation.json()["status"] == "REJECTED"
                assert invalid_activation.json()["report"][0]["code"] == "INTEGRITY_ERROR"

                unavailable = await _request_activation(api, second)
                assert unavailable.status_code == 503
                assert (await api.get("/api/v1/context")).json()["version"] == first
                assert (await _request_activation(api, second)).json()["status"] == "ACTIVE"
                assert seen[-1].baseline_version == first

                current = (await api.get("/api/v1/context", params=params)).json()
                assert current["version"] == second
                assert current["corpus_fragments"][0]["text"] == "License catalogue for contractors"
                pinned = await api.get("/api/v1/context", params={**params, "version": first})
                assert pinned.json() == first_context

                regression = (await _request_activation(api, rejected)).json()
                assert regression["status"] == "REJECTED"
                assert regression["report"][0]["code"] == "REGRESSION_DETECTED"

                activation = asyncio.create_task(_request_activation(api, racing))
                try:
                    await asyncio.wait_for(called.wait(), timeout=10)
                    rollback = await api.post(f"/api/v1/versions/{first}/rollback")
                    assert rollback.json()["status"] == "ACTIVE"
                finally:
                    resume.set()
                assert (await activation).status_code == 409
                assert (await api.get("/api/v1/context", params=params)).json() == first_context
                assert (await _request_activation(api, racing)).json()["status"] == "ACTIVE"
                assert seen[-1].baseline_version == first
