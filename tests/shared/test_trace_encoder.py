from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans as encode_spans_with_opentelemetry
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import (
    Link,
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
    set_span_in_context,
)
from opentelemetry.trace.span import TraceState

from apitally.shared.span_processor import copy_span_with_attributes
from apitally.shared.trace_encoder import encode_spans


def test_encoded_spans_match_opentelemetry_encoder():
    exporter = InMemorySpanExporter()
    providers = [
        TracerProvider(resource=Resource({"service.name": name}, schema_url="https://opentelemetry.io/schemas/1.0"))
        for name in ("a", "b")
    ]
    for provider in providers:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    remote_parent = SpanContext(
        trace_id=1, span_id=2, is_remote=True, trace_flags=TraceFlags(1), trace_state=TraceState([("k", "v")])
    )
    tracer = providers[0].get_tracer("server", "1.0", attributes={"scope.attribute": "x"})
    with tracer.start_as_current_span(
        "GET /items",
        context=set_span_in_context(NonRecordingSpan(remote_parent)),
        kind=SpanKind.SERVER,
        links=[Link(remote_parent, {"link.attribute": 1})],
        attributes={"string": "s", "bool": True, "int": 1, "float": 1.5, "strings": ["a", "b"], "empty": []},
    ) as span:
        span.add_event("event", {"event.attribute": "e"}, timestamp=123)
        span.set_status(Status(StatusCode.ERROR, "failed"))
        with providers[0].get_tracer("client").start_as_current_span("GET", kind=SpanKind.CLIENT):
            pass
    with providers[1].get_tracer("server").start_as_current_span("POST /items", kind=SpanKind.SERVER):
        pass
    spans = list(exporter.get_finished_spans())
    # Apitally's span exporter adds bytes values and lists to the spans it copies
    spans[1] = copy_span_with_attributes(spans[1], {**(spans[1].attributes or {}), "body": b"\x00", "list": ["x"]})

    assert ExportTraceServiceRequest.FromString(encode_spans(spans)) == encode_spans_with_opentelemetry(spans)
