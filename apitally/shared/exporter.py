import json
import logging
from collections.abc import Callable, Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from apitally.shared.capture import BODY_TOO_LARGE, MAX_BODY_SIZE, CaptureMixin
from apitally.shared.redaction import REDACTED
from apitally.shared.span_processor import BODIES_ATTRIBUTE, copy_span_with_attributes


logger = logging.getLogger(__name__)

QUERY_ATTRIBUTES = ("url.query", "http.target", "http.url", "url.full")
HEADER_ATTRIBUTE_PREFIXES = ("http.request.header.", "http.response.header.")


class ApitallySpanExporter(SpanExporter, CaptureMixin):
    """Applies redaction and body processing on the export thread, in front of the delegate exporter."""

    def __init__(self, delegate: SpanExporter) -> None:
        self.delegate = delegate
        self.bind_config()

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
        """Return a copy with redaction applied and body attributes added. The copy does not
        carry the raw bodies, and the original span is never mutated."""
        request_body, response_body = getattr(span, BODIES_ATTRIBUTE, (None, None))
        has_bodies = request_body is not None or response_body is not None
        if not has_bodies and not any(
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
        if request_body is not None:
            attributes["apitally.request.body"] = self.process_body(
                span, request_body, self.config.mask_request_body, "mask_request_body"
            )
            changed = True
        if response_body is not None:
            attributes["apitally.response.body"] = self.process_body(
                span, response_body, self.config.mask_response_body, "mask_response_body"
            )
            changed = True
        return copy_span_with_attributes(span, attributes) if changed else span

    def process_body(
        self,
        span: ReadableSpan,
        body: bytes,
        mask_callback: Callable[[ReadableSpan, bytes], bytes | None] | None,
        callback_name: str,
    ) -> str:
        if mask_callback is not None:
            try:
                masked = mask_callback(span, body)
            except Exception:
                logger.warning(
                    "Apitally %s callback raised an exception, body replaced with %s",
                    callback_name,
                    REDACTED,
                    exc_info=True,
                )
                masked = None
            if masked is None:
                return REDACTED
            if not isinstance(masked, bytes):
                logger.warning(
                    "Apitally %s callback returned an invalid value, body replaced with %s", callback_name, REDACTED
                )
                return REDACTED
            if len(masked) > MAX_BODY_SIZE:
                return BODY_TOO_LARGE
            body = masked
        try:
            data = json.loads(body)
        except Exception:
            # Non-JSON but allowlisted (e.g. text/plain): stored as-is
            return body.decode("utf-8", errors="replace")
        try:
            return json.dumps(self.redaction.redact_body(data), separators=(",", ":"), ensure_ascii=False)
        except Exception:
            logger.warning("Error redacting body, replaced with %s", REDACTED, exc_info=True)
            return REDACTED

    def shutdown(self) -> None:
        self.delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.delegate.force_flush(timeout_millis)
