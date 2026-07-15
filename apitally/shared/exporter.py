import json
import logging
from collections.abc import Callable, Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from apitally.shared.config import BODY_TOO_LARGE, MAX_BODY_SIZE, get_config
from apitally.shared.redaction import REDACTED, Redaction
from apitally.shared.sentry import SENTRY_EVENT_ID_ATTRIBUTE, pop_sentry_event_id
from apitally.shared.span_processor import STASH_ATTRIBUTE, RequestStash, copy_span_with_attributes


logger = logging.getLogger(__name__)

QUERY_ATTRIBUTES = ("url.query", "http.target", "http.url", "url.full")
HEADER_ATTRIBUTE_PREFIXES = ("http.request.header.", "http.response.header.")


class ApitallySpanExporter(SpanExporter):
    """Applies redaction and attaches stashed headers, bodies and Sentry event IDs on the
    export thread, in front of the delegate exporter."""

    def __init__(self, delegate: SpanExporter) -> None:
        self.delegate = delegate
        self.config = get_config()
        self.redaction = Redaction(
            self.config.mask_query_params, self.config.mask_headers, self.config.mask_body_fields
        )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        processed = []
        for span in spans:
            try:
                processed.append(self.process_span(span))
            except Exception:  # pragma: no cover
                # A span that failed redaction must never leave the process
                logger.exception("Error processing span for export, span dropped")
        return self.delegate.export(processed)

    def process_span(self, span: ReadableSpan) -> ReadableSpan:
        """Return a copy with redaction applied and stashed headers, bodies and Sentry event IDs
        added as attributes. The copy does not carry the stash, and the original span is never mutated."""
        stash: RequestStash | None = getattr(span, STASH_ATTRIBUTE, None)
        context = span.get_span_context()
        sentry_event_id = pop_sentry_event_id(context.span_id) if context is not None else None
        if (
            stash is None
            and sentry_event_id is None
            and not any(
                key in QUERY_ATTRIBUTES or key.startswith(HEADER_ATTRIBUTE_PREFIXES) for key in span.attributes or {}
            )
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
        if sentry_event_id is not None:
            attributes[SENTRY_EVENT_ID_ATTRIBUTE] = sentry_event_id
            changed = True
        if stash is None:
            return copy_span_with_attributes(span, attributes) if changed else span
        for prefix, headers in (
            ("http.request.header.", stash.request_headers),
            ("http.response.header.", stash.response_headers),
        ):
            if headers:
                for name, values in self.redaction.redact_headers(headers).items():
                    attributes[prefix + name] = values
        # The mask callbacks receive the span as it will be exported, minus the body attributes
        span = copy_span_with_attributes(span, dict(attributes))
        if stash.request_body is None and stash.response_body is None:
            return span
        if stash.request_body is not None:
            attributes["apitally.request.body"] = self.process_body(
                span, stash.request_body, self.config.mask_request_body, "mask_request_body"
            )
        if stash.response_body is not None:
            attributes["apitally.response.body"] = self.process_body(
                span, stash.response_body, self.config.mask_response_body, "mask_response_body"
            )
        return copy_span_with_attributes(span, attributes)

    def process_body(
        self,
        span: ReadableSpan,
        body: bytes,
        mask_callback: Callable[[ReadableSpan, bytes], bytes | None] | None,
        callback_name: str,
    ) -> str | bytes:
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
            if not isinstance(masked, bytes):  # pragma: no cover
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
            try:
                return body.decode("utf-8")
            except UnicodeDecodeError:
                return body
        try:
            return json.dumps(self.redaction.redact_body(data), separators=(",", ":"), ensure_ascii=False)
        except Exception:  # pragma: no cover
            logger.warning("Error redacting body, replaced with %s", REDACTED, exc_info=True)
            return REDACTED

    def shutdown(self) -> None:
        self.delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # pragma: no cover
        return self.delegate.force_flush(timeout_millis)
