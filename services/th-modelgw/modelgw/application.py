import hashlib
import json
import logging
from asyncio import to_thread
from dataclasses import dataclass
from time import monotonic
from typing import Protocol
from uuid import UUID

from pydantic import JsonValue, ValidationError

from modelgw.assessment import assess_risk
from modelgw.context import PreparedContext, prepare_context
from modelgw.errors import Failure, GatewayError
from modelgw.journal import Invocation, InvocationResult, Journal
from modelgw.privacy import redact_public_text
from modelgw.prompts import system_prompt
from modelgw.schemas import (
    Contract,
    EmbeddingAnswer,
    Identifier,
    InferenceRequest,
    InvokeResponse,
    ModelEmbeddings,
    ModelRisk,
    ModelText,
    RiskAnswer,
    Task,
    TextAnswer,
)

_LOG = logging.getLogger("uvicorn.error.modelgw")


@dataclass(frozen=True, slots=True)
class ModelCall:
    task: Task
    model: str
    prompt: str | None
    texts: tuple[str, ...]
    image_url: str | None


class ModelReply(Contract):
    model_version: Identifier
    output: ModelRisk | ModelText | ModelEmbeddings


class ModelProvider(Protocol):
    async def infer(self, call: ModelCall) -> tuple[str, JsonValue]: ...


class Cache(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> None: ...


class Gateway:
    def __init__(
        self,
        provider: ModelProvider | None,
        cache: Cache,
        journal: Journal,
        models: dict[Task, str | None],
        prompt_version: str,
        confidence_floor: float,
        cache_namespace: str,
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._journal = journal
        self._models = models
        self._prompt_version = prompt_version
        self._confidence_floor = confidence_floor
        self._cache_namespace = cache_namespace

    async def infer(self, request: InferenceRequest, trace_id: UUID) -> InvokeResponse:
        started = monotonic()
        model = self._models[request.task]
        provider = self._provider
        context: PreparedContext | None = None
        reply: ModelReply | None = None
        response: InvokeResponse | None = None
        request_hash: str | None = None
        attempt: int | None = None
        cache_hit = False
        outcome = "internal_error"
        try:
            if request.prompt_version != self._prompt_version:
                raise GatewayError(Failure.UNSUPPORTED_PROMPT)
            if model is None or provider is None:
                raise GatewayError(Failure.TASK_DISABLED)
            context = await to_thread(prepare_context, request)
            call = ModelCall(
                task=request.task,
                model=model,
                prompt=system_prompt(self._prompt_version, request.task),
                texts=context.texts,
                image_url=context.image_url,
            )
            request_hash = self._request_hash(call)
            attempt = await self._journal.begin(
                Invocation(
                    request.job_id,
                    trace_id,
                    request.task,
                    request.prompt_version,
                    context.knowledge.rules_version if context.knowledge else None,
                    model,
                    request_hash,
                ),
                context.replacements,
            )
            reply, cache_hit = await self._model_reply(provider, call, request_hash)
            answer, confidence = self._answer(call, context, reply)
            response = InvokeResponse(
                job_id=request.job_id,
                answer=answer,
                confidence=confidence,
                model_version=reply.model_version,
                prompt_version=self._prompt_version,
                cached=cache_hit,
                trace_id=trace_id,
            )
            outcome = "ok"
            return response
        except GatewayError as error:
            outcome = error.code.value
            raise
        finally:
            knowledge = context.knowledge if context is not None else None
            answer = response.answer if response is not None else None
            human_reason = (
                answer.human_reason if isinstance(answer, RiskAnswer | TextAnswer) else None
            )
            result = InvocationResult(
                outcome=outcome,
                duration_ms=int((monotonic() - started) * 1000),
                model_version=reply.model_version if reply else None,
                cached=cache_hit,
                confidence=response.confidence if response else None,
                human_reason=human_reason,
            )
            try:
                if attempt is None and outcome in {
                    Failure.INVALID_CONTEXT,
                    Failure.UNSAFE_CONTEXT,
                    Failure.UNSUPPORTED_PROMPT,
                    Failure.TASK_DISABLED,
                }:
                    attempt = await self._journal.begin(
                        Invocation(
                            request.job_id,
                            trace_id,
                            request.task,
                            request.prompt_version,
                            knowledge.rules_version if knowledge else None,
                            model,
                            request_hash,
                        ),
                        (),
                    )
                if attempt is not None:
                    await self._journal.finish(attempt, result)
            except GatewayError:
                _LOG.warning("model_journal trace_id=%s outcome=storage_unavailable", trace_id)
                if outcome == "ok":
                    raise
            _LOG.info(
                "inference trace_id=%s job_id=%s task=%s prompt_version=%s rules_version=%s "
                "model_version=%s cached=%s outcome=%s human_reason=%s confidence=%s duration_ms=%d",
                trace_id,
                request.job_id,
                request.task,
                request.prompt_version,
                knowledge.rules_version if knowledge else None,
                reply.model_version if reply else model,
                cache_hit,
                outcome,
                human_reason,
                response.confidence if response else None,
                result.duration_ms,
            )

    def _request_hash(self, call: ModelCall) -> str:
        request_key = json.dumps(
            [
                self._cache_namespace,
                self._prompt_version,
                call.task,
                call.model,
                call.prompt,
                call.texts,
                call.image_url,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(request_key.encode()).hexdigest()

    async def _model_reply(
        self, provider: ModelProvider, call: ModelCall, request_hash: str
    ) -> tuple[ModelReply, bool]:
        key = f"model:cache:{request_hash}"
        cached = await self._cache.get(key)
        if cached is not None:
            try:
                reply = ModelReply.model_validate_json(cached)
                self._validate_output(call, reply.output)
            except ValidationError, GatewayError:
                _LOG.warning("model_cache outcome=invalid_entry")
            else:
                return reply, True

        version, raw = await provider.infer(call)
        try:
            match call.task:
                case "risk_assessment":
                    output = ModelRisk.model_validate(raw)
                case "embeddings":
                    output = ModelEmbeddings.model_validate(raw)
                case _:
                    output = ModelText.model_validate(raw)
            self._validate_output(call, output)
            reply = ModelReply(model_version=version, output=output)
        except ValidationError:
            raise GatewayError(Failure.INVALID_PROVIDER_RESPONSE) from None
        await self._cache.set(key, reply.model_dump_json())
        return reply, False

    @staticmethod
    def _validate_output(call: ModelCall, output: ModelRisk | ModelText | ModelEmbeddings) -> None:
        try:
            match call.task, output:
                case "embeddings", ModelEmbeddings() as embeddings:
                    if (
                        len(embeddings.vectors) == len(call.texts)
                        and len({len(v) for v in embeddings.vectors}) == 1
                    ):
                        return
                case "risk_assessment", ModelRisk() as risk:
                    if all(source <= len(call.texts) for source in risk.sources):
                        risk.summary = redact_public_text(risk.summary)
                        return
                case (("vision" | "document_draft"), ModelText() as text):
                    text.text = redact_public_text(text.text)
                    return
        except GatewayError:
            raise GatewayError(Failure.INVALID_PROVIDER_RESPONSE) from None
        raise GatewayError(Failure.INVALID_PROVIDER_RESPONSE)

    def _answer(
        self, call: ModelCall, context: PreparedContext, reply: ModelReply
    ) -> tuple[RiskAnswer | TextAnswer | EmbeddingAnswer, float]:
        match call.task, reply.output:
            case "risk_assessment", ModelRisk() as output if context.knowledge and context.penalty:
                return assess_risk(
                    output, context.knowledge, context.penalty, self._confidence_floor
                )
            case "embeddings", ModelEmbeddings() as output:
                return EmbeddingAnswer(
                    vectors=output.vectors, source_ids=list(range(1, len(call.texts) + 1))
                ), 1.0
            case ("vision" | "document_draft") as kind, ModelText() as output:
                human_reason = (
                    "unclear_requirements" if output.confidence < self._confidence_floor else None
                )
                return TextAnswer(
                    kind=kind, text=output.text, human_reason=human_reason
                ), output.confidence
        raise GatewayError(Failure.INVALID_PROVIDER_RESPONSE)
