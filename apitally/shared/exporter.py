import logging
from collections.abc import Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from apitally.shared.config import ApitallyConfig, get_config
from apitally.shared.redaction import REDACTED, Redaction
from apitally.shared.span_processor import copy_span_with_attributes


logger = logging.getLogger(__name__)

QUERY_ATTRIBUTES = ("url.query", "http.target", "http.url", "url.full")
HEADER_ATTRIBUTE_PREFIXES = ("http.request.header.", "http.response.header.")


class ApitallySpanExporter(SpanExporter):
    """Applies redaction on the export thread, in front of the delegate exporter."""

    def __init__(self, delegate: SpanExporter) -> None:
        self.delegate = delegate
        self.config = get_config() or ApitallyConfig()
        self.redaction = Redaction(
            self.config.mask_query_params, self.config.mask_headers, self.config.mask_body_fields
        )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        processed = []
        for span in spans:
            try:
                processed.append(self.process_span(span))
            except Exception:
                # A span that failed redaction must never leave the process
                logger.exception("Error processing span for export, span dropped")
        return self.delegate.export(processed)

    def process_span(self, span: ReadableSpan) -> ReadableSpan:
        """Return a redacted copy when redaction is needed. The original span is never mutated."""
        if not any(
            key in QUERY_ATTRIBUTES or key.startswith(HEADER_ATTRIBUTE_PREFIXES) for key in span.attributes or {}
        ):
            return span
        attributes = dict(span.attributes or {})
        changed = False
        for key, value in attributes.items():
            if key in QUERY_ATTRIBUTES and isinstance(value, str):
                redacted = self.redaction.redact_query_params(value, assume_query=key == "url.query")
            elif key.startswith(HEADER_ATTRIBUTE_PREFIXES):
                header = key.removeprefix("http.request.header.").removeprefix("http.response.header.")
                if not self.redaction.should_redact_header(header):
                    continue
                redacted = REDACTED if isinstance(value, str) else [REDACTED]
            else:
                continue
            if redacted != value:
                attributes[key] = redacted
                changed = True
        return copy_span_with_attributes(span, attributes) if changed else span

    def shutdown(self) -> None:
        self.delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.delegate.force_flush(timeout_millis)
