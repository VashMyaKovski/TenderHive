from collections.abc import Mapping
from uuid import UUID

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from modelgw.application import Gateway
from modelgw.errors import GatewayError
from modelgw.schemas import InferenceRequest, InvokeResponse


def tracer_provider(endpoint: str, timeout_seconds: float) -> TracerProvider:
    provider = TracerProvider(
        resource=Resource({"service.name": "th-modelgw"}), shutdown_on_exit=False
    )
    exporter = OTLPSpanExporter(endpoint=endpoint, timeout=timeout_seconds)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


async def traced_inference(
    tracer: Tracer,
    headers: Mapping[str, str],
    gateway: Gateway,
    body: InferenceRequest,
    trace_id: UUID,
) -> InvokeResponse:
    with tracer.start_as_current_span(
        "modelgw.invoke",
        context=TraceContextTextMapPropagator().extract(headers),
        kind=SpanKind.SERVER,
        attributes={
            "tender.trace_id": str(trace_id),
            "tender.job_id": str(body.job_id),
            "modelgw.task": body.task,
            "modelgw.prompt_version": body.prompt_version,
        },
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            response = await gateway.infer(body, trace_id)
        except GatewayError as error:
            span.set_attribute("modelgw.error", error.code.value)
            span.set_status(StatusCode.ERROR)
            raise
        span.set_attribute("modelgw.cached", response.cached)
        span.set_attribute("modelgw.model_version", response.model_version)
        return response
