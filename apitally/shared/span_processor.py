from __future__ import annotations

import logging
from collections.abc import Callable
from contextvars import ContextVar

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
from opentelemetry.trace import SpanKind

from apitally.shared.config import ApitallyConfig, get_config
from apitally.shared.redaction import REDACTED, Redaction, compile_patterns, matches_any


logger = logging.getLogger(__name__)

DEFAULT_EXCLUDE_PATH_PATTERNS = [
    r"/_?healthz?$",
    r"/_?health[-_]?checks?$",
    r"/_?heart[-_]?beats?$",
    r"/ping$",
    r"/ready$",
    r"/live$",
    r"/favicon(?:-[\w-]+)?\.(ico|png|svg)$",
    r"/apple-touch-icon(?:-[\w-]+)?\.png$",
    r"/robots\.txt$",
    r"/sitemap\.xml$",
    r"/manifest\.json$",
    r"/site\.webmanifest$",
    r"/service-worker\.js$",
    r"/sw\.js$",
    r"/\.well-known/",
]
EXCLUDE_USER_AGENT_PATTERNS = compile_patterns(
    [
        r"health[-_ ]?check",
        r"microsoft-azure-application-lb",
        r"googlehc",
        r"kube-probe",
    ]
)

QUERY_ATTRIBUTES = ("url.query", "http.target", "http.url", "url.full")
HEADER_ATTRIBUTE_PREFIXES = ("http.request.header.", "http.response.header.")
NOISE_NAME_SUFFIXES = (" http send", " http receive", " websocket send", " websocket receive")
NOISE_SCOPE_PREFIX = "opentelemetry.instrumentation."
MAX_BUFFERED_SPANS = 1_000

server_span_var: ContextVar[Span | None] = ContextVar("apitally_server_span", default=None)
server_span_kept_var: ContextVar[bool] = ContextVar("apitally_server_span_kept", default=False)


def get_server_span() -> Span | None:
    return server_span_var.get()


def is_server_span_kept() -> bool:
    return server_span_kept_var.get()


def sampled_in(trace_id: int, bound: int) -> bool:
    # TraceIdRatioBased convention: the low 64 bits of the trace ID tested against round(rate * 2**64),
    # deterministic per trace so services sampling at the same rate capture the same traces
    return trace_id & TraceIdRatioBased.TRACE_ID_LIMIT < bound


class ApitallySpanProcessor(SpanProcessor):
    """Single keep/drop mechanism in front of the wrapped export processor (design.md section 3)."""

    def __init__(self, downstream: SpanProcessor) -> None:
        # Settable so fork re-activation can swap in a fresh batch processor (design.md section 7)
        self.downstream = downstream
        self.spans: dict[int, tuple[bool, int | None]] = {}
        self.pending: dict[int, list[ReadableSpan]] = {}
        # Assigned by the log processor so both buffers flush or discard on the same decision
        self.on_request_finished: Callable[[int, bool], None] | None = None
        self.config = get_config() or ApitallyConfig()
        self.sample_rate_bound = TraceIdRatioBased.get_bound_for_rate(self.config.sample_rate)
        self.exclude_path_patterns = compile_patterns(DEFAULT_EXCLUDE_PATH_PATTERNS + self.config.exclude_paths)
        self.redaction = Redaction(
            self.config.mask_query_params, self.config.mask_headers, self.config.mask_body_fields
        )

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        try:
            if span.context is None:
                return
            if is_noise_span(span):
                self.spans[span.context.span_id] = (False, None)
            elif span.parent is None or span.parent.is_remote:
                if span.kind == SpanKind.SERVER:
                    server_span_var.set(span)
                    keep = not self.exclude_request(span) and self.sample_request(span, span.context.trace_id)
                    server_span_kept_var.set(keep)
                    self.spans[span.context.span_id] = (keep, span.context.span_id if keep else None)
                    if keep:
                        self.pending[span.context.span_id] = []
                else:
                    self.spans[span.context.span_id] = (False, None)
            else:
                self.spans[span.context.span_id] = self.spans.get(span.parent.span_id, (False, None))
        except Exception:
            logger.exception("Error in Apitally span processor")

    def on_end(self, span: ReadableSpan) -> None:
        try:
            context = span.get_span_context()
            if context is None:
                return
            keep, server_span_id = self.spans.pop(context.span_id, (False, None))
            if not keep:
                return
            buffer = self.pending.pop(context.span_id, None)
            if buffer is not None:
                # Pending SERVER root: the response-stage decision flushes or discards the whole request
                kept = self.sample_response(span, context.trace_id)
                if kept:
                    for buffered_span in buffer:
                        self.downstream.on_end(self.redact_span(buffered_span))
                    self.downstream.on_end(self.redact_span(span))
                else:
                    # Flip in-flight entries so the request's late telemetry drops locally (design.md section 3)
                    for span_id, entry in list(self.spans.items()):
                        if entry[1] == context.span_id:
                            self.spans[span_id] = (False, None)
                if self.on_request_finished is not None:
                    self.on_request_finished(context.span_id, kept)
                return
            pending = self.pending.get(server_span_id) if server_span_id is not None else None
            if pending is not None:
                if len(pending) < MAX_BUFFERED_SPANS:
                    pending.append(span)
                else:
                    logger.debug("Apitally span buffer cap reached for request, dropping span")
                return
            self.downstream.on_end(self.redact_span(span))
        except Exception:
            logger.exception("Error in Apitally span processor")

    def resolve_server_span_id(self, span_id: int) -> int | None:
        """Return the SERVER span id for an in-flight span, or None if the request is dropped."""
        entry = self.spans.get(span_id)
        return entry[1] if entry else None

    def shutdown(self) -> None:
        # Pending requests' SERVER spans can never export after shutdown, so their telemetry is unreachable
        self.pending.clear()
        self.downstream.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.downstream.force_flush(timeout_millis)

    def exclude_request(self, span: Span) -> bool:
        attributes = span.attributes or {}
        method = attributes.get("http.request.method") or attributes.get("http.method")
        if method == "OPTIONS":
            return True
        path = attributes.get("url.path") or attributes.get("http.target")
        if path and matches_any(self.exclude_path_patterns, str(path).partition("?")[0]):
            return True
        user_agent = attributes.get("user_agent.original") or attributes.get("http.user_agent")
        if user_agent and matches_any(EXCLUDE_USER_AGENT_PATTERNS, str(user_agent)):
            return True
        return False

    def sample_request(self, span: Span, trace_id: int) -> bool:
        rate = self.resolve_sample_rate(self.config.sample_on_request, span, "sample_on_request")
        if rate is None:
            return sampled_in(trace_id, self.sample_rate_bound)
        return sampled_in(trace_id, TraceIdRatioBased.get_bound_for_rate(rate))

    def sample_response(self, span: ReadableSpan, trace_id: int) -> bool:
        # An unconfigured callback or an abstaining None leaves the request-stage decision standing
        rate = self.resolve_sample_rate(self.config.sample_on_response, span, "sample_on_response")
        return rate is None or sampled_in(trace_id, TraceIdRatioBased.get_bound_for_rate(rate))

    def resolve_sample_rate(
        self, callback: Callable[[ReadableSpan], float | bool | None] | None, span: ReadableSpan, name: str
    ) -> float | None:
        """Map a sampling callback result to an effective rate; None means no callback or it abstained."""
        if callback is None:
            return None
        try:
            result = callback(span)
        except Exception:
            logger.warning("Apitally %s callback raised an exception, request captured", name, exc_info=True)
            return 1.0
        if result is None:
            return None
        if isinstance(result, bool):
            return 1.0 if result else 0.0
        if isinstance(result, (int, float)) and 0 <= result <= 1:
            return float(result)
        logger.warning("Apitally %s callback returned an invalid value, request captured: %r", name, result)
        return 1.0

    def redact_span(self, span: ReadableSpan) -> ReadableSpan:
        """Return a copy with query params and headers redacted; the original span is never mutated."""
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
        if not changed:
            return span
        return ReadableSpan(
            name=span.name,
            context=span.get_span_context(),
            parent=span.parent,
            resource=span.resource,
            attributes=attributes,
            events=span.events,
            links=span.links,
            kind=span.kind,
            status=span.status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )


def is_noise_span(span: Span) -> bool:
    # Spec section 6.6 backstop; user-owned spans with these names are kept (design.md section 3)
    return (
        span.kind == SpanKind.INTERNAL
        and span.name.endswith(NOISE_NAME_SUFFIXES)
        and span.instrumentation_scope is not None
        and span.instrumentation_scope.name.startswith(NOISE_SCOPE_PREFIX)
    )
