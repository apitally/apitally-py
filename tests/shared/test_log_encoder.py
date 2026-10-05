from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs as encode_logs_with_opentelemetry
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace import INVALID_SPAN, NonRecordingSpan, SpanContext, TraceFlags, set_span_in_context

from apitally.shared.log_encoder import encode_logs


def test_encoded_logs_match_opentelemetry_encoder():
    exporter = InMemoryLogRecordExporter()
    providers = [
        LoggerProvider(resource=Resource({"service.name": name}, schema_url="https://opentelemetry.io/schemas/1.0"))
        for name in ("a", "b")
    ]
    for provider in providers:
        provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    span_context = SpanContext(trace_id=1, span_id=2, is_remote=False, trace_flags=TraceFlags(1))
    app_logger = providers[0].get_logger("app", "1.0", "https://example.com", attributes={"scope.attribute": "x"})
    app_logger.emit(
        timestamp=1,
        observed_timestamp=2,
        context=set_span_in_context(NonRecordingSpan(span_context)),
        severity_number=SeverityNumber.WARN,
        severity_text="WARN",
        body="message",
        attributes={
            "string": "s",
            "bool": True,
            "int": 1,
            "float": 1.5,
            "tuple": ("a", "b"),
            "dict": {"k": "v"},
            "empty": {},
        },
    )
    # Apitally's own events carry dict bodies and no trace context
    apitally_logger = providers[0].get_logger("apitally")
    for body in ({"counts": [{"count": 1}], "attributes": {"plan": "pro"}}, "{}"):
        apitally_logger.emit(timestamp=3, context=set_span_in_context(INVALID_SPAN), body=body, event_name="event")
    providers[1].get_logger("app").emit(body="message")
    records = exporter.get_finished_logs()

    assert ExportLogsServiceRequest.FromString(encode_logs(records)) == encode_logs_with_opentelemetry(records)
