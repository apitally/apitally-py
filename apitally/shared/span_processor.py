from __future__ import annotations

import logging
import re
from collections.abc import Callable
from contextvars import ContextVar

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
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

server_span_var: ContextVar[Span | None] = ContextVar("apitally_server_span", default=None)


def get_server_span() -> Span | None:
    return server_span_var.get()


class ApitallySpanProcessor(SpanProcessor):
    """Single keep/drop mechanism in front of the wrapped export processor (design.md section 3)."""

    def __init__(self, downstream: SpanProcessor) -> None:
        # Settable so fork re-activation can swap in a fresh batch processor (design.md section 7)
        self.downstream = downstream
        self.spans: dict[int, tuple[bool, int | None]] = {}
        self.config: ApitallyConfig | None = None
        self.exclude_path_patterns: list[re.Pattern[str]] = []
        self.redaction = Redaction()
        self.refresh_config()

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        try:
            if span.context is None:
                return
            if is_noise_span(span):
                self.spans[span.context.span_id] = (False, None)
            elif span.parent is None or span.parent.is_remote:
                if span.kind == SpanKind.SERVER:
                    server_span_var.set(span)
                    keep = not self.exclude_request(span)
                    self.spans[span.context.span_id] = (keep, span.context.span_id if keep else None)
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
            keep, _ = self.spans.pop(context.span_id, (False, None))
            if not keep:
                return
            config = self.refresh_config()
            if span.kind == SpanKind.SERVER and self.callback_excludes(
                config.exclude_on_response, span, "exclude_on_response"
            ):
                return
            self.downstream.on_end(self.redact_span(span))
        except Exception:
            logger.exception("Error in Apitally span processor")

    def resolve_server_span_id(self, span_id: int) -> int | None:
        """Return the SERVER span id for an in-flight span, or None if the request is dropped."""
        entry = self.spans.get(span_id)
        return entry[1] if entry else None

    def shutdown(self) -> None:
        self.downstream.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.downstream.force_flush(timeout_millis)

    def exclude_request(self, span: Span) -> bool:
        config = self.refresh_config()
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
        return self.callback_excludes(config.exclude_on_request, span, "exclude_on_request")

    def callback_excludes(self, callback: Callable[[ReadableSpan], bool] | None, span: ReadableSpan, name: str) -> bool:
        if callback is None:
            return False
        try:
            return bool(callback(span))
        except Exception:
            logger.warning("Apitally %s callback raised an exception, request not excluded", name, exc_info=True)
            return False

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

    def refresh_config(self) -> ApitallyConfig:
        config = get_config() or ApitallyConfig()
        if config is not self.config:
            self.config = config
            self.exclude_path_patterns = compile_patterns(DEFAULT_EXCLUDE_PATH_PATTERNS + config.exclude_paths)
            self.redaction = Redaction(config.mask_query_params, config.mask_headers, config.mask_body_fields)
        return config


def is_noise_span(span: Span) -> bool:
    # Spec section 6.6 backstop; user-owned spans with these names are kept (design.md section 3)
    return (
        span.kind == SpanKind.INTERNAL
        and span.name.endswith(NOISE_NAME_SUFFIXES)
        and span.instrumentation_scope is not None
        and span.instrumentation_scope.name.startswith(NOISE_SCOPE_PREFIX)
    )
