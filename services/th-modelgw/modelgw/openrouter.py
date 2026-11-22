import asyncio
import logging

import httpx
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from modelgw.application import ModelCall
from modelgw.errors import Failure, GatewayError
from modelgw.schemas import EmbeddingVector


class _Message(BaseModel):
    content: str


class _Choice(BaseModel):
    finish_reason: str
    message: _Message


class _Completion(BaseModel):
    model: str
    choices: list[_Choice]


class _Embedding(BaseModel):
    index: int
    embedding: EmbeddingVector


class _Embeddings(BaseModel):
    model: str
    data: list[_Embedding]


_JSON = TypeAdapter(JsonValue)
_LOG = logging.getLogger("uvicorn.error.modelgw")


class OpenRouter:
    def __init__(
        self,
        client: httpx.AsyncClient,
        timeout: float,
        max_retries: int,
        retry_delay: float,
        max_completion_tokens: int,
        synthetic_provider_collection: bool,
    ) -> None:
        self._client = client
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._max_completion_tokens = max_completion_tokens
        self._synthetic_provider_collection = synthetic_provider_collection

    async def infer(self, call: ModelCall) -> tuple[str, JsonValue]:
        collection = "allow" if call.synthetic and self._synthetic_provider_collection else "deny"
        if call.task == "embeddings":
            response = await self._post(
                "embeddings",
                {
                    "model": call.model,
                    "input": list(call.texts),
                    "encoding_format": "float",
                    "provider": {"data_collection": collection},
                },
            )
            return self._embeddings(response)

        sources: list[JsonValue] = [
            {"source_id": index, "text": text} for index, text in enumerate(call.texts, 1)
        ]
        evidence = _JSON.dump_json({"evidence": sources}).decode("utf-8")
        user_parts: list[JsonValue] = [{"type": "text", "text": evidence}]
        if call.image_url is not None:
            user_parts.append({"type": "image_url", "image_url": {"url": call.image_url}})
        messages: list[JsonValue] = [
            {"role": "system", "content": call.prompt},
            {"role": "user", "content": user_parts if call.image_url else evidence},
        ]
        response = await self._post(
            "chat/completions",
            {
                "model": call.model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "max_tokens": self._max_completion_tokens,
                "provider": {"data_collection": collection, "require_parameters": True},
            },
        )
        return self._completion(response)

    async def _post(self, path: str, payload: dict[str, JsonValue]) -> bytes:
        for attempt in range(self._max_retries + 1):
            try:
                async with asyncio.timeout(self._timeout):
                    response = await self._client.post(path, json=payload)
            except TimeoutError, httpx.TimeoutException:
                failure = Failure.PROVIDER_TIMEOUT
            except httpx.TransportError:
                failure = Failure.PROVIDER_UNAVAILABLE
            else:
                if response.is_success:
                    return response.content
                _LOG.info("model_provider operation=%s http_status=%d", path, response.status_code)
                if response.status_code not in {408, 429} and not response.is_server_error:
                    raise GatewayError(Failure.PROVIDER_REJECTED)
                failure = (
                    Failure.PROVIDER_TIMEOUT
                    if response.status_code == 408
                    else Failure.PROVIDER_UNAVAILABLE
                )

            if attempt == self._max_retries:
                raise GatewayError(failure)
            await asyncio.sleep(self._retry_delay * 2**attempt)

        raise GatewayError(Failure.PROVIDER_UNAVAILABLE)

    @staticmethod
    def _completion(response: bytes) -> tuple[str, JsonValue]:
        try:
            result = _Completion.model_validate_json(response)
            if len(result.choices) != 1 or result.choices[0].finish_reason != "stop":
                raise GatewayError(Failure.INVALID_PROVIDER_RESPONSE)
            return result.model, _JSON.validate_json(result.choices[0].message.content)
        except ValidationError:
            raise GatewayError(Failure.INVALID_PROVIDER_RESPONSE) from None

    @staticmethod
    def _embeddings(response: bytes) -> tuple[str, JsonValue]:
        try:
            result = _Embeddings.model_validate_json(response)
        except ValidationError:
            raise GatewayError(Failure.INVALID_PROVIDER_RESPONSE) from None
        chunks = sorted(result.data, key=lambda chunk: chunk.index)
        if [chunk.index for chunk in chunks] != list(range(len(chunks))):
            raise GatewayError(Failure.INVALID_PROVIDER_RESPONSE)
        vectors: list[JsonValue] = [list(chunk.embedding) for chunk in chunks]
        return result.model, {"vectors": vectors}
