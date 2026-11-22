import json
import logging
from asyncio import sleep
from base64 import b64encode
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from io import BytesIO
from uuid import UUID, uuid4

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from modelgw.api import create_app, gateway_for
from modelgw.application import Gateway
from modelgw.config import Settings
from modelgw.openrouter import OpenRouter

PATH = "/api/v1/inference"
SETTINGS = {
    "OPENROUTER_API_KEY": "synthetic-secret",
    "OPENROUTER_MODEL_TEST": "test/chat",
    "openrouter_url": "https://router.invalid/api/v1/",
    "synthetic_provider_collection": True,
    "embeddings_model": "test/embedding",
    "vision_model": None,
    "prompt_version": "v1",
    "confidence_floor": 0.65,
    "timeout_seconds": 0.02,
    "max_retries": 2,
    "retry_delay_seconds": 0.001,
    "max_completion_tokens": 256,
    "redis_url": "redis://localhost:6379/0",
    "redis_timeout_seconds": 0.5,
    "cache_ttl_seconds": 1800,
    "bind_host": "127.0.0.1",
    "port": 8000,
}


class MemoryCache:
    def __init__(self) -> None:
        self.entries: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.entries.get(key)

    async def set(self, key: str, value: str) -> None:
        self.entries[key] = value


@asynccontextmanager
async def client_for(
    handler: Callable[[httpx.Request], httpx.Response],
    cache: MemoryCache,
    vision_model: str | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings.model_validate({**SETTINGS, "vision_model": vision_model})
    async with httpx.AsyncClient(
        base_url=str(settings.openrouter_url),
        transport=httpx.MockTransport(handler),
        headers={"Authorization": f"Bearer {settings.openrouter_api_key.get_secret_value()}"},
    ) as upstream:
        service = Gateway(
            OpenRouter(upstream, 0.02, 2, 0.001, 256, True),
            cache,
            settings.models(),
            "v1",
            0.65,
            "test",
        )
        app = create_app(settings)
        app.dependency_overrides[gateway_for] = lambda: service
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.invalid"
        ) as client:
            yield client


def body(task: str, context: Sequence[object]) -> dict[str, object]:
    return {
        "job_id": str(uuid4()),
        "task": task,
        "context": list(context),
        "prompt_version": "v1",
        "mask": True,
    }


def completion(output: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "test/chat-2026-01",
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output)}}],
        },
    )


PUBLIC_TEXT = {"type": "public_text", "text": "Delivery terms; Email: do-not-send@example.org"}


def test_empty_key_is_rejected_without_serializing_it() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings.model_validate({**SETTINGS, "OPENROUTER_API_KEY": "secret with space"})
    assert "secret with space" not in str(exc.value)


async def test_masking_precedes_provider_and_cached_hypothesis_rechecks_kb(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[httpx.Request] = []

    def upstream(call: httpx.Request) -> httpx.Response:
        calls.append(call)
        return completion(
            {
                "summary": "Possible delivery risk.",
                "risk_level": "low",
                "penalty_daily_percent": None,
                "confidence": 0.9,
                "sources": [1],
            }
        )

    caplog.set_level(logging.INFO, logger="uvicorn.error.modelgw")
    cache = MemoryCache()
    knowledge = {
        "type": "knowledge",
        "rules_version": "kb1",
        "penalty_daily_threshold_percent": "0.1",
    }
    context: list[object] = [
        PUBLIC_TEXT,
        knowledge,
        {"type": "penalty", "daily_percent": "0.15"},
        {"type": "sensitive_text", "category": "commercial", "text": "private payment term"},
    ]
    payload = body("risk_assessment", context)

    async with client_for(upstream, cache) as client:
        first = await client.post(PATH, json=payload)
        updated = {**knowledge, "rules_version": "kb2", "penalty_daily_threshold_percent": "0.2"}
        second = await client.post(
            PATH, json=body("risk_assessment", [context[0], updated, context[2]])
        )

    assert first.status_code == second.status_code == 200
    assert first.json()["answer"]["human_reason"] == "penalty_above_threshold"
    assert first.json()["confidence"] == 0.0
    assert second.json()["answer"]["rules_version"] == "kb2"
    assert second.json()["answer"]["human_reason"] is None
    assert first.json()["trace_id"] != second.json()["trace_id"]
    assert len(calls) == 1
    sent = calls[0].content.decode()
    for raw in (
        "do-not-send@example.org",
        "private payment term",
        str(payload["job_id"]),
        '"0.15"',
        "kb1",
    ):
        assert raw not in sent
        assert raw not in next(iter(cache.entries.values()))
    assert "synthetic-secret" not in caplog.text
    assert "do-not-send@example.org" not in caplog.text
    assert "rules_version=kb2" in caplog.text
    assert "[REDACTED_PERSONAL]" in sent
    assert json.loads(sent)["provider"]["data_collection"] == "deny"


async def test_collection_only_allowed_for_exclusively_synthetic_provider_input() -> None:
    routing: list[str] = []

    def upstream(call: httpx.Request) -> httpx.Response:
        routing.append(json.loads(call.content)["provider"]["data_collection"])
        return completion({"text": "Checklist", "confidence": 0.8})

    synthetic = {"type": "synthetic_text", "text": "Invent a delivery checklist"}
    public = {"type": "public_text", "text": synthetic["text"]}
    async with client_for(upstream, MemoryCache()) as client:
        for context in ([synthetic], [public], [public, synthetic]):
            result = await client.post(PATH, json=body("document_draft", context))
            assert result.status_code == 200

    assert routing == ["allow", "deny", "deny"]


async def test_vision_sends_inline_pixels_and_keeps_protected_routing() -> None:
    def upstream(call: httpx.Request) -> httpx.Response:
        payload = json.loads(call.content)
        assert payload["model"] == "test/vision"
        assert payload["provider"]["data_collection"] == "deny"
        image_part = payload["messages"][1]["content"][1]
        assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")
        return completion({"text": "Layout could not be resolved", "confidence": 0.3})

    raw = BytesIO()
    Image.new("RGB", (3, 3)).save(raw, format="PNG")
    context = [{"type": "public_image", "data": b64encode(raw.getvalue()).decode("ascii")}]
    async with client_for(upstream, MemoryCache(), vision_model="test/vision") as client:
        result = await client.post(PATH, json=body("vision", context))

    assert result.status_code == 200
    assert result.json()["answer"]["human_reason"] == "unclear_requirements"


async def test_invalid_request_and_unsafe_input_never_call_provider() -> None:
    def upstream(_: httpx.Request) -> httpx.Response:
        pytest.fail("request must not reach upstream")

    async with client_for(upstream, MemoryCache()) as client:
        unsafe = await client.post(
            PATH, json=body("document_draft", [{"type": "public_text", "text": "IBAN BY00PRIVATE"}])
        )
        invalid = await client.post(
            PATH, json={**body("document_draft", ["secret"]), "mask": False}
        )
        disabled = await client.post(PATH, json=body("vision", [PUBLIC_TEXT]))
        bad_prompt = await client.post(
            PATH, json={**body("document_draft", [PUBLIC_TEXT]), "prompt_version": "v2"}
        )

    assert unsafe.status_code == invalid.status_code == bad_prompt.status_code == 422
    assert unsafe.json()["code"] == "unsafe_context"
    assert invalid.json()["code"] == "invalid_request"
    assert bad_prompt.json()["code"] == "unsupported_prompt_version"
    assert disabled.status_code == 503
    assert disabled.json()["code"] == "task_disabled"
    assert "secret" not in invalid.text
    UUID(invalid.json()["trace_id"])


@pytest.mark.parametrize(
    ("upstream_status", "status", "code", "attempts"),
    [
        (429, 503, "provider_unavailable", 3),
        (503, 503, "provider_unavailable", 3),
        (408, 504, "provider_timeout", 3),
        (401, 502, "provider_rejected", 1),
        (200, 502, "invalid_provider_response", 1),
    ],
)
async def test_explicit_upstream_failures_are_not_cached(
    upstream_status: int,
    status: int,
    code: str,
    attempts: int,
) -> None:
    calls = 0

    def upstream(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(upstream_status, json={"raw_detail": "must_not_leak"})

    cache = MemoryCache()
    async with client_for(upstream, cache) as client:
        result = await client.post(PATH, json=body("document_draft", [PUBLIC_TEXT]))

    assert calls == attempts
    assert result.status_code == status
    assert result.json()["code"] == code
    assert "must_not_leak" not in result.text
    assert not cache.entries


async def test_embeddings_are_ordered_and_tied_to_the_input() -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/embeddings"
        assert json.loads(request.content)["input"] == ["first", "second"]
        return httpx.Response(
            200,
            json={
                "model": "test/embedding",
                "data": [
                    {"index": 1, "embedding": [0.2, 0.3]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ],
            },
        )

    async with client_for(upstream, MemoryCache()) as client:
        result = await client.post(
            PATH,
            json=body(
                "embeddings",
                [
                    {"type": "public_text", "text": "first"},
                    {"type": "public_text", "text": "second"},
                ],
            ),
        )

    assert result.status_code == 200
    assert result.json()["answer"]["vectors"] == [[0.1, 0.2], [0.2, 0.3]]


async def test_timeout_covers_the_entire_attempt() -> None:
    async def upstream(_: httpx.Request) -> httpx.Response:
        await sleep(1)
        return completion({"text": "late", "confidence": 1.0})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(upstream), base_url="https://router.invalid"
    ) as client:
        from modelgw.application import ModelCall
        from modelgw.errors import Failure, GatewayError

        provider = OpenRouter(client, 0.01, 0, 0.001, 256, False)
        with pytest.raises(GatewayError) as exc:
            await provider.infer(
                ModelCall("document_draft", "test/chat", "JSON", ("text",), None, False)
            )

    assert exc.value.code == Failure.PROVIDER_TIMEOUT
