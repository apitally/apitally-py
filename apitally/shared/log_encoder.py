from collections.abc import Sequence

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.logs.v1.logs_pb2 import ResourceLogs, ScopeLogs
from opentelemetry.sdk._logs import ReadableLogRecord

from apitally.shared.trace_encoder import add_attributes, set_any_value


def encode_logs(records: Sequence[ReadableLogRecord]) -> bytes:
    """Encode log records as an OTLP ExportLogsServiceRequest. Equivalent to OpenTelemetry's encode_logs, but builds
    every message in place, which avoids its intermediate messages and their copies into each parent."""
    request = ExportLogsServiceRequest()
    # Grouped by object identity, since hashing a Resource serializes its attributes to JSON
    resource_logs_by_id: dict[int, ResourceLogs] = {}
    scope_logs_by_ids: dict[tuple[int, int], ScopeLogs] = {}
    for record in records:
        resource = record.resource
        scope = record.instrumentation_scope
        scope_logs = scope_logs_by_ids.get((id(resource), id(scope)))
        if scope_logs is None:
            resource_logs = resource_logs_by_id.get(id(resource))
            if resource_logs is None:
                resource_logs = request.resource_logs.add(schema_url=resource.schema_url)
                add_attributes(resource_logs.resource.attributes, resource.attributes)
                resource_logs_by_id[id(resource)] = resource_logs
            scope_logs = resource_logs.scope_logs.add()
            if scope is not None:
                scope_logs.scope.name = scope.name
                scope_logs.scope.version = scope.version or ""
                scope_logs.schema_url = scope.schema_url or ""
                add_attributes(scope_logs.scope.attributes, scope.attributes)
            scope_logs_by_ids[(id(resource), id(scope))] = scope_logs

        log_record = record.log_record
        encoded_log_record = scope_logs.log_records.add(
            time_unix_nano=log_record.timestamp,
            observed_time_unix_nano=log_record.observed_timestamp,
            severity_number=log_record.severity_number.value if log_record.severity_number else None,
            severity_text=log_record.severity_text,
            dropped_attributes_count=record.dropped_attributes,
            flags=log_record.trace_flags,
            trace_id=log_record.trace_id.to_bytes(16, "big") if log_record.trace_id else None,
            span_id=log_record.span_id.to_bytes(8, "big") if log_record.span_id else None,
            event_name=log_record.event_name,
        )
        set_any_value(encoded_log_record.body, log_record.body)
        add_attributes(encoded_log_record.attributes, log_record.attributes)
    return request.SerializeToString()
