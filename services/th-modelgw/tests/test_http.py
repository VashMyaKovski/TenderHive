import json
import logging
from asyncio import sleep
from base64 import b64encode
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from io import BytesIO
from uuid import UUID, uuid4

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer
from PIL import Image
from pydantic import ValidationError

from modelgw.api import create_app, gateway_for
from modelgw.application import Gateway, ModelCall
from modelgw.config import Settings
from modelgw.context import Replacement
from modelgw.errors import Failure, GatewayError
from modelgw.journal import Invocation, InvocationResult
from modelgw.provider import OpenAICompatibleProvider

PATH = "/api/v1/inference"
INVOKE_PATH = "/api/v1/invoke"
SETTINGS = {
    "provider": "nvidia",
    "NVIDIA_API_KEY": "synthetic-secret",
    "MODEL_RISK": "test/chat",
    "nvidia_base_url": "https://integrate.api.nvidia.com/v1/",
    "embedding_input_type": "query",
    "embeddings_model": "test/embedding",
    "vision_model": None,
    "prompt_version": "v1",
    "confidence_floor": 0.65,
    "timeout_seconds": 0.02,
    "max_retries": 2,
    "retry_delay_seconds": 0.001,
    "max_completion_tokens": 256,
    "redis_url": "redis://localhost:6379/0",
    "postgres_dsn": "postgresql://modelgw:local-test@localhost/modelgw",
    "masking_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "db_timeout_seconds": 3,
    "pii_masking_enabled": True,
    "log_level": "INFO",
    "otlp_endpoint": None,
    "otlp_timeout_seconds": 0.5,
    "redis_timeout_seconds": 0.5,
    "cache_ttl_seconds": 1800,
    "bind_host": "127.0.0.1",
    "port": 8000,
}
DISABLED_SETTINGS = {
    **SETTINGS,
    "NVIDIA_API_KEY": "",
    "MODEL_RISK": "",
    "embeddings_model": "",
    "vision_model": "",
    "masking_key": "",
}
OPENROUTER_SETTINGS = {
    **SETTINGS,
    "provider": "openrouter",
    "OPENROUTER_API_KEY": "synthetic-openrouter-secret",
    "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1/",
    "OPENROUTER_MODEL_TEST": "test/chat",
    "OPENROUTER_EMBEDDING_MODEL": "test/embedding",
}


class MemoryCache:
    def __init__(self) -> None:
        self.entries: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.entries.get(key)

    async def set(self, key: str, value: str) -> None:
        self.entries[key] = value


class MemoryJournal:
    def __init__(self) -> None:
        self.attempts: list[tuple[Invocation, tuple[Replacement, ...]]] = []
        self.results: dict[int, InvocationResult] = {}

    async def ready(self, version: str) -> None:
        assert version == "v1"

    async def begin(self, invocation: Invocation, replacements: tuple[Replacement, ...]) -> int:
        self.attempts.append((invocation, replacements))
        return len(self.attempts)

    async def finish(self, attempt: int, result: InvocationResult) -> None:
        self.results[attempt] = result


@asynccontextmanager
async def client_for(
    handler: Callable[[httpx.Request], httpx.Response],
    cache: MemoryCache,
    vision_model: str | None = None,
    journal: MemoryJournal | None = None,
    prompt_version: str = "v1",
    tracer: Tracer | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings.model_validate(
        {**SETTINGS, "vision_model": vision_model, "prompt_version": prompt_version}
    )
    api_key = settings.nvidia_api_key
    assert api_key is not None
    async with httpx.AsyncClient(
        base_url=str(settings.nvidia_base_url),
        transport=httpx.MockTransport(handler),
        headers={"Authorization": f"Bearer {api_key.get_secret_value()}"},
    ) as upstream:
        service = Gateway(
            OpenAICompatibleProvider(
                upstream,
                0.02,
                2,
                0.001,
                256,
                settings.embedding_input_type,
                request_json_output=False,
            ),
            cache,
            journal if journal is not None else MemoryJournal(),
            settings.models(),
            settings.prompt_version,
            0.65,
            "test",
        )
        app = create_app(settings)
        app.dependency_overrides[gateway_for] = lambda: service
        if tracer is not None:
            app.state.tracer = tracer
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


def test_invalid_key_is_rejected_without_serializing_it() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings.model_validate({**SETTINGS, "NVIDIA_API_KEY": "secret with space"})
    assert "secret with space" not in str(exc.value)


@pytest.mark.parametrize("model", ["MODEL_RISK", "embeddings_model", "vision_model"])
@pytest.mark.parametrize("missing_key", ["NVIDIA_API_KEY", "masking_key"])
def test_each_enabled_model_requires_both_keys(model: str, missing_key: str) -> None:
    payload = {
        **DISABLED_SETTINGS,
        model: "test/model",
        "NVIDIA_API_KEY": SETTINGS["NVIDIA_API_KEY"],
        "masking_key": SETTINGS["masking_key"],
        missing_key: "",
    }
    with pytest.raises(ValidationError, match="required to enable a model"):
        Settings.model_validate(payload)


async def test_disabled_model_server_starts_and_validates_without_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = MemoryJournal()
    monkeypatch.setattr("modelgw.api.journal_for", lambda _: journal)

    def unexpected_provider(*_: object) -> None:
        pytest.fail("a provider must not be created when the models are disabled")

    monkeypatch.setattr("modelgw.api.OpenAICompatibleProvider", unexpected_provider)
    settings = Settings.model_validate(DISABLED_SETTINGS)
    assert settings.nvidia_api_key is settings.masking_key is None
    assert all(model is None for model in settings.models().values())

    request = {**body("document_draft", [PUBLIC_TEXT]), "trace_id": str(uuid4())}
    app = create_app(settings)
    async with app.router.lifespan_context(app) as state:
        assert state is not None
        app.dependency_overrides[gateway_for] = lambda: state["gateway"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.invalid"
        ) as client:
            assert (await client.get("/readyz")).status_code == 200
            bad_prompt = await client.post(INVOKE_PATH, json={**request, "prompt_version": "v2"})
            disabled = await client.post(INVOKE_PATH, json=request)
    assert bad_prompt.status_code == 422
    assert bad_prompt.json()["code"] == "unsupported_prompt_version"
    assert disabled.status_code == 503
    assert disabled.json()["code"] == "task_disabled"
    assert [result.outcome for result in journal.results.values()] == [
        "unsupported_prompt_version",
        "task_disabled",
    ]
    assert all(not replacements for _, replacements in journal.attempts)


@pytest.mark.parametrize(
    "url",
    [
        "https://invalid.example/v1/",
        "https://integrate.api.nvidia.com.invalid.example/v1/",
        "https://integrate.api.nvidia.com:8443/v1/",
        "http://integrate.api.nvidia.com/v1/",
        "https://user:password@integrate.api.nvidia.com/v1/",
    ],
)
def test_settings_limit_bearer_token_destination(url: str) -> None:
    with pytest.raises(ValidationError) as exc:
        Settings.model_validate({**SETTINGS, "nvidia_base_url": url})
    assert "password" not in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://integrate.api.nvidia.com/v1/",
        "https://openrouter.ai.invalid.example/api/v1/",
        "https://openrouter.ai:8443/api/v1/",
        "http://openrouter.ai/api/v1/",
        "https://user:password@openrouter.ai/api/v1/",
        "https://openrouter.ai/v1/",
        "https://openrouter.ai/api/v1/?key=value",
        "https://openrouter.ai/api/v1/#fragment",
    ],
)
def test_openrouter_rejects_unapproved_bearer_token_destinations(url: str) -> None:
    with pytest.raises(ValidationError) as exc:
        Settings.model_validate({**OPENROUTER_SETTINGS, "OPENROUTER_BASE_URL": url})
    assert "password" not in str(exc.value)


@pytest.mark.parametrize(
    ("missing", "expected_error"),
    [
        ("OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),
        ("OPENROUTER_BASE_URL", "OPENROUTER_BASE_URL"),
        ("masking_key", "MODEL_GW_MASKING_KEY"),
    ],
)
def test_openrouter_requires_its_own_credentials_and_endpoint(
    missing: str, expected_error: str
) -> None:
    with pytest.raises(ValidationError, match=expected_error) as exc:
        Settings.model_validate({**OPENROUTER_SETTINGS, missing: ""})
    assert "synthetic-openrouter-secret" not in str(exc.value)
    assert "synthetic-secret" not in str(exc.value)


def test_openrouter_has_no_fallback_to_nvidia_models() -> None:
    settings = Settings.model_validate(
        {
            **OPENROUTER_SETTINGS,
            "OPENROUTER_MODEL_TEST": "",
            "OPENROUTER_EMBEDDING_MODEL": "",
            "vision_model": "test/vision",
        }
    )
    assert all(model is None for model in settings.models().values())


async def test_openrouter_lifespan_uses_distinct_cache_and_sends_only_masked_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = MemoryJournal()
    cache = MemoryCache()
    calls: list[tuple[str, str, str | None, dict[str, object]]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.host is not None
        payload = json.loads(request.content)
        calls.append(
            (request.url.host, request.url.path, request.headers.get("Authorization"), payload)
        )
        assert "do-not-send@example.org" not in request.content.decode()
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200, json={"model": "test/embedding", "data": [{"index": 0, "embedding": [0.2]}]}
            )
        return completion({"text": "Checklist", "confidence": 0.9})

    async_client = httpx.AsyncClient

    def mock_provider_client(
        *,
        base_url: str,
        headers: dict[str, str],
        timeout: float,
        follow_redirects: bool,
        trust_env: bool,
    ) -> httpx.AsyncClient:
        assert follow_redirects is trust_env is False
        return async_client(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=follow_redirects,
            trust_env=trust_env,
            transport=httpx.MockTransport(upstream),
        )

    monkeypatch.setattr("modelgw.api.journal_for", lambda _: journal)
    monkeypatch.setattr("modelgw.api.RedisCache", lambda *_: cache)
    monkeypatch.setattr("modelgw.api.httpx.AsyncClient", mock_provider_client)

    trace_id = str(uuid4())
    draft = {**body("document_draft", [PUBLIC_TEXT]), "trace_id": trace_id}

    async def exercise_provider(provider_settings: Mapping[str, object]) -> None:
        app = create_app(Settings.model_validate(provider_settings))
        async with app.router.lifespan_context(app) as state:
            assert state is not None
            gateway = state["gateway"]
            app.dependency_overrides[gateway_for] = lambda: gateway
            async with async_client(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway.invalid"
            ) as client:
                response = await client.post(INVOKE_PATH, json=draft)
                assert response.status_code == 200
                assert response.headers["X-Trace-Id"] == trace_id
                assert response.json()["cached"] is False
                if provider_settings is OPENROUTER_SETTINGS:
                    cached = await client.post(INVOKE_PATH, json=draft)
                    embedded = await client.post(PATH, json=body("embeddings", [PUBLIC_TEXT]))
                    assert cached.json()["cached"] is True
                    assert embedded.status_code == 200
                    assert embedded.json()["answer"]["vectors"] == [[0.2]]

    await exercise_provider(SETTINGS)
    await exercise_provider(OPENROUTER_SETTINGS)

    nvidia_chat, openrouter_chat, openrouter_embeddings = calls
    assert nvidia_chat[:3] == (
        "integrate.api.nvidia.com",
        "/v1/chat/completions",
        "Bearer synthetic-secret",
    )
    assert "response_format" not in nvidia_chat[3]
    assert openrouter_chat[:3] == (
        "openrouter.ai",
        "/api/v1/chat/completions",
        "Bearer synthetic-openrouter-secret",
    )
    assert openrouter_chat[3]["response_format"] == {"type": "json_object"}
    assert openrouter_embeddings[:3] == (
        "openrouter.ai",
        "/api/v1/embeddings",
        "Bearer synthetic-openrouter-secret",
    )
    assert openrouter_embeddings[3]["model"] == "test/embedding"
    assert "input_type" not in openrouter_embeddings[3]
    assert len(cache.entries) == 3
    assert str(journal.attempts[1][0].trace_id) == trace_id
    assert journal.attempts[1][1]
    assert journal.results[2].outcome == "ok"


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
    assert "cached" not in first.json()
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
    assert "provider" not in json.loads(sent)


async def test_invoke_preserves_trace_and_journals_replacements_before_upstream() -> None:
    journal = MemoryJournal()
    trace_id = str(uuid4())
    cache = MemoryCache()

    def upstream(call: httpx.Request) -> httpx.Response:
        assert call.url.path == "/v1/chat/completions"
        assert call.headers["Authorization"] == "Bearer synthetic-secret"
        assert "do-not-send@example.org" not in call.content.decode()
        invocation, replacements = journal.attempts[0]
        assert str(invocation.trace_id) == trace_id
        assert replacements == (
            Replacement(1, "[REDACTED_PERSONAL]", "Email: do-not-send@example.org"),
            Replacement(2, "[REDACTED_COMMERCIAL]", "private payment term"),
        )
        return completion({"text": "Checklist", "confidence": 0.8})

    context = [
        PUBLIC_TEXT,
        {"type": "sensitive_text", "category": "commercial", "text": "private payment term"},
    ]
    payload = {**body("document_draft", context), "trace_id": trace_id}
    async with client_for(upstream, cache, journal=journal) as client:
        fresh = await client.post(INVOKE_PATH, json=payload)
        hit = await client.post(INVOKE_PATH, json=payload)
    assert fresh.status_code == hit.status_code == 200
    assert fresh.json()["trace_id"] == fresh.headers["X-Trace-Id"] == trace_id
    assert fresh.json()["cached"] is False
    assert hit.json()["cached"] is True
    assert journal.results[1].outcome == "ok"
    assert journal.results[1].cached is False
    assert journal.results[2].cached is True
    assert list(cache.entries) == [f"model:cache:{journal.attempts[0][0].request_hash}"]


async def test_journal_failure_prevents_upstream_request() -> None:
    class DownJournal(MemoryJournal):
        async def begin(self, invocation: Invocation, replacements: tuple[Replacement, ...]) -> int:
            raise GatewayError(Failure.STORAGE_UNAVAILABLE)

    def upstream(_: httpx.Request) -> httpx.Response:
        pytest.fail("an invocation without a journal entry must not reach upstream")

    payload = {**body("document_draft", [PUBLIC_TEXT]), "trace_id": str(uuid4())}
    async with client_for(upstream, MemoryCache(), journal=DownJournal()) as client:
        result = await client.post(INVOKE_PATH, json=payload)
    assert result.status_code == 503
    assert result.json()["code"] == "storage_unavailable"
    assert result.json()["trace_id"] == payload["trace_id"]


async def test_invocation_span_continues_parent_without_exposing_context() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:

        def upstream(_: httpx.Request) -> httpx.Response:
            return completion({"text": "Checklist", "confidence": 0.8})

        caller_trace = str(uuid4())
        payload = {**body("document_draft", [PUBLIC_TEXT]), "trace_id": caller_trace}
        traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        async with client_for(
            upstream, MemoryCache(), tracer=provider.get_tracer(__name__)
        ) as client:
            result = await client.post(
                INVOKE_PATH, json=payload, headers={"traceparent": traceparent}
            )
        assert result.status_code == 200
        (span,) = exporter.get_finished_spans()
        assert span.parent is not None
        assert span.parent.span_id == int("00f067aa0ba902b7", 16)
        assert span.attributes is not None
        assert span.attributes["tender.trace_id"] == caller_trace
        assert span.attributes["modelgw.cached"] is False
        assert "do-not-send@example.org" not in str(span.attributes)
    finally:
        provider.shutdown()


async def test_active_prompt_version_invalidates_cached_output() -> None:
    calls: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["messages"][0]["content"])
        return completion({"text": "Checklist", "confidence": 0.8})

    cache = MemoryCache()
    payload = {**body("document_draft", [PUBLIC_TEXT]), "trace_id": str(uuid4())}
    async with client_for(upstream, cache) as client:
        v1 = await client.post(INVOKE_PATH, json=payload)
    async with client_for(upstream, cache, prompt_version="v3") as client:
        v3 = await client.post(INVOKE_PATH, json={**payload, "prompt_version": "v3"})
    assert v1.status_code == v3.status_code == 200
    assert v1.json()["cached"] is v3.json()["cached"] is False
    assert len(calls) == len(set(calls)) == 2


async def test_vision_sends_inline_pixels() -> None:
    def upstream(call: httpx.Request) -> httpx.Response:
        payload = json.loads(call.content)
        assert payload["model"] == "test/vision"
        assert "provider" not in payload
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


async def test_invoke_validation_error_preserves_caller_trace() -> None:
    def upstream(_: httpx.Request) -> httpx.Response:
        pytest.fail("invalid request must not reach upstream")

    caller_trace = str(uuid4())
    payload = {**body("document_draft", ["private context"]), "trace_id": caller_trace}
    async with client_for(upstream, MemoryCache()) as client:
        invalid = await client.post(INVOKE_PATH, json=payload)
        missing = await client.post(
            INVOKE_PATH,
            json=body("document_draft", [PUBLIC_TEXT]),
            headers={"X-Trace-Id": caller_trace},
        )
    for result in (invalid, missing):
        assert result.status_code == 422
        assert result.json() == {
            "code": "invalid_request",
            "message": "The request does not match the required schema.",
            "trace_id": caller_trace,
        }
        assert "private context" not in result.text


@pytest.mark.parametrize("header_value", ["malformed", str(uuid4())])
async def test_invoke_rejects_mismatched_trace_header(header_value: str) -> None:
    def upstream(_: httpx.Request) -> httpx.Response:
        pytest.fail("mismatched trace IDs must be rejected before upstream")

    caller_trace = str(uuid4())
    body_with_trace = {**body("document_draft", [PUBLIC_TEXT]), "trace_id": caller_trace}
    async with client_for(upstream, MemoryCache()) as client:
        result = await client.post(
            INVOKE_PATH, json=body_with_trace, headers={"X-Trace-Id": header_value}
        )
    assert result.status_code == 422
    assert result.headers["X-Trace-Id"] == caller_trace
    assert result.json() == {
        "code": "invalid_request",
        "message": "X-Trace-Id must match the request trace_id.",
        "trace_id": caller_trace,
    }


@pytest.mark.parametrize(
    ("upstream_status", "status", "code", "reason", "attempts"),
    [
        (429, 503, "provider_unavailable", "rate_limit", 3),
        (503, 503, "provider_unavailable", "unavailable", 3),
        (408, 504, "provider_timeout", "timeout", 3),
        (504, 504, "provider_timeout", "timeout", 3),
        (202, 504, "provider_timeout", "timeout", 1),
        (401, 502, "provider_rejected", "auth", 1),
        (200, 502, "invalid_provider_response", "schema_mismatch", 1),
    ],
)
async def test_explicit_upstream_failures_are_not_cached(
    upstream_status: int,
    status: int,
    code: str,
    reason: str,
    attempts: int,
) -> None:
    calls = 0

    def upstream(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(upstream_status, json={"raw_detail": "must_not_leak"})

    cache = MemoryCache()
    payload = body("document_draft", [PUBLIC_TEXT])
    trace_id = str(uuid4())
    async with client_for(upstream, cache) as client:
        result = await client.post(PATH, json=payload)
        canonical = await client.post(INVOKE_PATH, json={**payload, "trace_id": trace_id})

    assert calls == attempts * 2
    assert result.status_code == canonical.status_code == status
    assert result.json()["code"] == code
    assert canonical.json() == {
        "job_id": payload["job_id"],
        "status": "UNAVAILABLE",
        "reason": reason,
        "trace_id": trace_id,
    }
    assert "must_not_leak" not in result.text
    assert not cache.entries


async def test_provider_failure_does_not_return_an_expired_cached_answer() -> None:
    calls = 0

    def upstream(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return completion({"text": "Previously generated checklist", "confidence": 0.8})
        return httpx.Response(503)

    cache = MemoryCache()
    payload = body("document_draft", [PUBLIC_TEXT])
    async with client_for(upstream, cache) as client:
        first = await client.post(PATH, json=payload)
        assert first.status_code == 200
        assert len(cache.entries) == 1

        cache.entries.clear()  # Redis GET returns a miss after the TTL expires.
        result = await client.post(PATH, json=payload)

    assert calls == 4
    assert result.status_code == 503
    assert result.json()["code"] == "provider_unavailable"
    assert "answer" not in result.json()
    assert not cache.entries


async def test_embeddings_are_ordered_and_tied_to_the_input() -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        assert json.loads(request.content) == {
            "model": "test/embedding",
            "input": ["first", "second"],
            "encoding_format": "float",
            "input_type": "query",
        }
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
        transport=httpx.MockTransport(upstream), base_url="https://nvidia.invalid"
    ) as client:
        provider = OpenAICompatibleProvider(
            client, 0.01, 0, 0.001, 256, None, request_json_output=False
        )
        with pytest.raises(GatewayError) as exc:
            await provider.infer(ModelCall("document_draft", "test/chat", "JSON", ("text",), None))

    assert exc.value.code == Failure.PROVIDER_TIMEOUT
