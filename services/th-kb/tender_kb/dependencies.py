import hashlib
import logging
import re
from typing import Annotated, Literal, Protocol
from uuid import UUID, uuid4

import httpx
from pydantic import Field, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from tender_kb.domain import (
    ContextProjection,
    Contract,
    DependencyUnavailable,
    EvalRequest,
    EvalResult,
)

_LOG = logging.getLogger(__name__)
EVAL_HEADER = "X-KB-Eval-Token"


class HttpGate:
    def __init__(self, client: httpx.AsyncClient, url: str, token: str, timeout: int) -> None:
        self._client = client
        self._url = f"{url.rstrip('/')}/api/v1/kb/check"
        self._token = token
        self._timeout = timeout

    async def check(self, request: EvalRequest) -> EvalResult:
        try:
            response = await self._client.post(
                self._url,
                headers={EVAL_HEADER: self._token},
                json=request.model_dump(mode="json"),
                timeout=self._timeout,
            )
            return EvalResult.model_validate_json(response.raise_for_status().content)
        except (httpx.HTTPError, ValidationError) as error:
            raise DependencyUnavailable("Knowledge evaluation is unavailable.") from error


class Embeddings(Protocol):
    async def encode(self, texts: list[str]) -> list[list[float]]: ...


class _EmbeddingAnswer(Contract):
    kind: Literal["embeddings"]
    vectors: list[list[Annotated[float, Field(strict=True, allow_inf_nan=False)]]]
    source_ids: list[int]


class _EmbeddingResponse(Contract):
    job_id: UUID
    answer: _EmbeddingAnswer
    confidence: float
    model_version: str
    prompt_version: str
    masked: Literal[True]
    trace_id: UUID
    cached: bool


class GatewayEmbeddings:
    def __init__(
        self, client: httpx.AsyncClient, url: str, prompt_version: str, vector_size: int
    ) -> None:
        self._client = client
        self._url = f"{url.rstrip('/')}/api/v1/invoke"
        self._prompt_version = prompt_version
        self._vector_size = vector_size

    async def encode(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        # The gateway's v1 contract allows at most twelve input items per request.
        for start in range(0, len(texts), 12):
            batch = texts[start : start + 12]
            job_id, trace_id = uuid4(), uuid4()
            try:
                response = await self._client.post(
                    self._url,
                    json={
                        "job_id": str(job_id),
                        "trace_id": str(trace_id),
                        "task": "embeddings",
                        "context": [{"type": "public_text", "text": text} for text in batch],
                        "prompt_version": self._prompt_version,
                        "mask": True,
                    },
                )
                payload = _EmbeddingResponse.model_validate_json(
                    response.raise_for_status().content
                )
            except (httpx.HTTPError, ValidationError) as error:
                raise DependencyUnavailable("Knowledge embeddings are unavailable.") from error
            if (
                payload.job_id != job_id
                or payload.trace_id != trace_id
                or payload.prompt_version != self._prompt_version
                or len(payload.answer.vectors) != len(batch)
                or any(len(vector) != self._vector_size for vector in payload.answer.vectors)
            ):
                raise DependencyUnavailable("The model gateway returned incompatible embeddings.")
            vectors.extend(payload.answer.vectors)
        return vectors


class FixtureEmbeddings:
    def __init__(self, vector_size: int) -> None:
        self._vector_size = vector_size

    async def encode(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self._vector_size
            tokens = re.findall(r"\w+", text.casefold()) or [text]
            for token in tokens:
                bucket = int.from_bytes(hashlib.sha256(token.encode()).digest()[:8])
                vector[bucket % self._vector_size] += 1.0
            vectors.append(vector)
        return vectors


class RedisContextCache:
    def __init__(self, client: Redis, ttl: int) -> None:
        self._client = client
        self._ttl = ttl

    async def get(self, key: str) -> ContextProjection | None:
        try:
            raw = await self._client.get(key)
            return ContextProjection.model_validate_json(raw) if raw is not None else None
        except (RedisError, ValidationError) as error:
            _LOG.warning("Knowledge context cache read failed (%s)", type(error).__name__)
            return None

    async def put(self, key: str, context: ContextProjection) -> None:
        try:
            await self._client.set(key, context.model_dump_json(), ex=self._ttl)
        except RedisError as error:
            _LOG.warning("Knowledge context cache write failed (%s)", type(error).__name__)
