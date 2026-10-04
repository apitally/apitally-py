from collections.abc import Mapping, Sequence
from typing import Any, cast

from google.protobuf.internal.containers import RepeatedCompositeFieldContainer
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, SpanFlags
from opentelemetry.proto.trace.v1.trace_pb2 import Span as EncodedSpan
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanContext, SpanKind


SPAN_KINDS = {
    SpanKind.INTERNAL: EncodedSpan.SPAN_KIND_INTERNAL,
    SpanKind.SERVER: EncodedSpan.SPAN_KIND_SERVER,
    SpanKind.CLIENT: EncodedSpan.SPAN_KIND_CLIENT,
    SpanKind.PRODUCER: EncodedSpan.SPAN_KIND_PRODUCER,
    SpanKind.CONSUMER: EncodedSpan.SPAN_KIND_CONSUMER,
}
FLAGS_PARENT_NOT_REMOTE = SpanFlags.SPAN_FLAGS_CONTEXT_HAS_IS_REMOTE_MASK
FLAGS_PARENT_REMOTE = FLAGS_PARENT_NOT_REMOTE | SpanFlags.SPAN_FLAGS_CONTEXT_IS_REMOTE_MASK


def encode_spans(spans: Sequence[ReadableSpan]) -> bytes:
    """Encode spans as an OTLP ExportTraceServiceRequest. Equivalent to OpenTelemetry's encode_spans, but builds
    every message in place, which avoids its intermediate messages and their copies into each parent."""
    request = ExportTraceServiceRequest()
    # Grouped by object identity, since hashing a Resource serializes its attributes to JSON
    resource_spans_by_id: dict[int, ResourceSpans] = {}
    scope_spans_by_ids: dict[tuple[int, int], ScopeSpans] = {}
    for span in spans:
        resource = span.resource
        scope = span.instrumentation_scope
        scope_spans = scope_spans_by_ids.get((id(resource), id(scope)))
        if scope_spans is None:
            resource_spans = resource_spans_by_id.get(id(resource))
            if resource_spans is None:
                resource_spans = request.resource_spans.add(schema_url=resource.schema_url)
                add_attributes(resource_spans.resource.attributes, resource.attributes)
                resource_spans_by_id[id(resource)] = resource_spans
            scope_spans = resource_spans.scope_spans.add()
            if scope is not None:
                scope_spans.scope.name = scope.name
                scope_spans.scope.version = scope.version or ""
                scope_spans.schema_url = scope.schema_url or ""
                add_attributes(scope_spans.scope.attributes, scope.attributes)
            scope_spans_by_ids[(id(resource), id(scope))] = scope_spans

        context = cast(SpanContext, span.get_span_context())
        parent = span.parent
        encoded_span = scope_spans.spans.add(
            trace_id=context.trace_id.to_bytes(16, "big"),
            span_id=context.span_id.to_bytes(8, "big"),
            trace_state=context.trace_state.to_header(),
            parent_span_id=parent.span_id.to_bytes(8, "big") if parent else None,
            flags=get_span_flags(parent),
            name=span.name,
            kind=SPAN_KINDS[span.kind],
            start_time_unix_nano=span.start_time,
            end_time_unix_nano=span.end_time,
            dropped_attributes_count=span.dropped_attributes,
            dropped_events_count=span.dropped_events,
            dropped_links_count=span.dropped_links,
        )
        add_attributes(encoded_span.attributes, span.attributes)
        encoded_span.status.code = span.status.status_code.value
        if span.status.description:
            encoded_span.status.message = span.status.description
        for event in span.events:
            encoded_event = encoded_span.events.add(
                name=event.name,
                time_unix_nano=event.timestamp,
                dropped_attributes_count=event.dropped_attributes,
            )
            add_attributes(encoded_event.attributes, event.attributes)
        for link in span.links:
            encoded_link = encoded_span.links.add(
                trace_id=link.context.trace_id.to_bytes(16, "big"),
                span_id=link.context.span_id.to_bytes(8, "big"),
                flags=get_span_flags(link.context),
                dropped_attributes_count=link.dropped_attributes,
            )
            add_attributes(encoded_link.attributes, link.attributes)
    return request.SerializeToString()


def add_attributes(key_values: RepeatedCompositeFieldContainer[KeyValue], attributes: Mapping[str, Any] | None) -> None:
    if attributes:
        for key, value in attributes.items():
            set_any_value(key_values.add(key=key).value, value)


def set_any_value(any_value: AnyValue, value: Any) -> None:
    # Attribute values are primitives or sequences of primitives; bool is checked before its superclass int
    if isinstance(value, str):
        any_value.string_value = value
    elif isinstance(value, (list, tuple)):
        values = any_value.array_value.values
        any_value.array_value.SetInParent()
        for item in value:
            set_any_value(values.add(), item)
    elif isinstance(value, bool):
        any_value.bool_value = value
    elif isinstance(value, int):
        any_value.int_value = value
    elif isinstance(value, float):
        any_value.double_value = value
    elif isinstance(value, bytes):
        any_value.bytes_value = value


def get_span_flags(parent: SpanContext | None) -> int:
    return FLAGS_PARENT_REMOTE if parent is not None and parent.is_remote else FLAGS_PARENT_NOT_REMOTE
