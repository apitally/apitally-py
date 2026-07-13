import logging
import sys
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache, partial
from typing import Any

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
from opentelemetry.trace import SpanContext, SpanKind
from opentelemetry.util.types import AttributeValue

from apitally.shared.config import ApitallyConfig, get_config
from apitally.shared.redaction import combine_patterns, compile_patterns, matches_any


if sys.version_info >= (3, 11):
    _exception_group_types: tuple[type[BaseExceptionGroup], ...] = (BaseExceptionGroup,)  # noqa: F821
else:
    try:
        from exceptiongroup import BaseExceptionGroup as _BaseExceptionGroup

        _exception_group_types = (_BaseExceptionGroup,)
    except ImportError:  # pragma: no cover
        _exception_group_types = ()

logger = logging.getLogger(__name__)

DEFAULT_EXCLUDE_PATH_PATTERN = combine_patterns(
    [
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
)
EXCLUDE_USER_AGENT_PATTERN = combine_patterns(
    [
        r"health[-_ ]?check",
        r"microsoft-azure-application-lb",
        r"googlehc",
        r"kube-probe",
    ]
)

MAX_BUFFERED_SPANS = 1_000
MAX_STASHED_REQUESTS = 2_048
STASH_ATTRIBUTE = "_apitally_stash"


@dataclass(slots=True)
class RequestStash:
    """Headers and bodies captured by a transport for one request, held until the SERVER span is exported."""

    request_headers: dict[str, list[str]] | None = None
    request_body: bytes | None = None
    response_headers: dict[str, list[str]] | None = None
    response_body: bytes | None = None


server_span_var: ContextVar[Span | None] = ContextVar("apitally_server_span", default=None)
server_span_kept_var: ContextVar[bool] = ContextVar("apitally_server_span_kept", default=False)
server_span_processor_var: ContextVar["ApitallySpanProcessor | None"] = ContextVar(
    "apitally_server_span_processor", default=None
)


def get_server_span() -> Span | None:
    return server_span_var.get()


def is_server_span_kept() -> bool:
    return server_span_kept_var.get()


def get_server_span_processor() -> "ApitallySpanProcessor | None":
    return server_span_processor_var.get()


def is_sampled_in(trace_id: int, bound: int) -> bool:
    # TraceIdRatioBased convention: the low 64 bits of the trace ID tested against round(rate * 2**64),
    # deterministic per trace so services sampling at the same rate capture the same traces
    return trace_id & TraceIdRatioBased.TRACE_ID_LIMIT < bound


def record_collapsed_exception(
    record_exception: Callable[..., None], exception: BaseException, *args: Any, **kwargs: Any
) -> None:
    """Unwraps single-leaf ExceptionGroups before recording the exception on the span."""
    while isinstance(exception, _exception_group_types) and len(exception.exceptions) == 1:  # ty: ignore[unresolved-attribute]
        exception = exception.exceptions[0]  # ty: ignore[unresolved-attribute]
    record_exception(exception, *args, **kwargs)


class ApitallySpanProcessor(SpanProcessor):
    """Acts as a single keep/drop mechanism in front of the wrapped export processor."""

    def __init__(self, downstream: SpanProcessor) -> None:
        # Settable so fork re-activation can swap in a fresh batch processor
        self.downstream = downstream
        self.spans: dict[int, tuple[bool, int | None]] = {}
        self.pending: dict[int, list[ReadableSpan]] = {}
        self.deferred: set[int] = set()
        self.held: dict[int, ReadableSpan] = {}
        self.stash: dict[int, RequestStash] = {}
        # Assigned by the log processor so both buffers flush or discard on the same decision
        self.on_request_finished: Callable[[int, bool], None] | None = None
        self.config = get_config() or ApitallyConfig()
        self.sample_rate_bound = TraceIdRatioBased.get_bound_for_rate(self.config.sample_rate)
        self.exclude_path_patterns = compile_patterns(self.config.exclude_paths)

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        # Imported here to break the circular import with consumer.py
        from apitally.shared.consumer import consumer_holder_var, write_consumer_span_attributes

        try:
            if span.context is None:  # pragma: no cover
                return
            if is_contrib_receive_send_span(span):
                self.spans[span.context.span_id] = (False, None)
            elif span.parent is None or span.parent.is_remote:
                if span.kind == SpanKind.SERVER:
                    server_span_var.set(span)
                    server_span_processor_var.set(self)
                    span.record_exception = partial(record_collapsed_exception, span.record_exception)
                    holder = consumer_holder_var.get()
                    if holder is not None and holder.identifier:
                        # Consumer set by middleware outside the transport middleware, before this span existed
                        write_consumer_span_attributes(span, holder)
                    keep = not self.exclude_request(span) and self.sample_request(span, span.context.trace_id)
                    server_span_kept_var.set(keep)
                    self.spans[span.context.span_id] = (keep, span.context.span_id if keep else None)
                    if keep:
                        self.pending[span.context.span_id] = []
                else:
                    self.spans[span.context.span_id] = (False, None)
            else:
                self.spans[span.context.span_id] = self.spans.get(span.parent.span_id, (False, None))
        except Exception:  # pragma: no cover
            logger.exception("Error in Apitally span processor")

    def on_end(self, span: ReadableSpan) -> None:
        try:
            context = span.get_span_context()
            if context is None:  # pragma: no cover
                return
            if context.span_id in self.deferred:
                self.deferred.discard(context.span_id)
                if self.spans.get(context.span_id, (False, None))[0]:
                    # Hold until finish_export; the spans and pending entries stay alive meanwhile
                    self.held[context.span_id] = span
                    return
            self.process_ended_span(span, context)
        except Exception:  # pragma: no cover
            logger.exception("Error in Apitally span processor")

    def defer_export(self, span_id: int) -> None:
        """Called by a transport while the SERVER span is still recording, committing to a later finish_export."""
        self.deferred.add(span_id)

    def finish_export(self, span_id: int, extra_attributes: Mapping[str, AttributeValue] | None = None) -> None:
        """Called by a transport when the response is complete, releasing a deferred SERVER span."""
        try:
            if span_id in self.deferred:
                # The span has not ended yet; write directly and let on_end export as usual
                self.deferred.discard(span_id)
                span = get_server_span()
                if (
                    extra_attributes
                    and span is not None
                    and span.context is not None
                    and span.context.span_id == span_id
                    and span.is_recording()
                ):
                    for key, value in extra_attributes.items():
                        span.set_attribute(key, value)
                return
            span = self.held.pop(span_id, None)
            if span is None:
                return
            if extra_attributes:
                span = copy_span_with_attributes(span, {**(span.attributes or {}), **extra_attributes})
            context = span.get_span_context()
            if context is not None:
                self.process_ended_span(span, context)
        except Exception:  # pragma: no cover
            logger.exception("Error in Apitally span processor")

    def update_stash(
        self,
        span_id: int,
        request_headers: dict[str, list[str]] | None = None,
        request_body: bytes | None = None,
        response_headers: dict[str, list[str]] | None = None,
        response_body: bytes | None = None,
    ) -> None:
        """Hold captured headers and bodies until process_ended_span attaches them to the exported
        SERVER span snapshot. Fields already stashed for the span are kept unless a new value is given."""
        entry = self.stash.get(span_id)
        if entry is None:
            if len(self.stash) >= MAX_STASHED_REQUESTS:  # pragma: no cover
                self.stash.pop(next(iter(self.stash)))
                logger.debug("Apitally request stash cap reached, dropping oldest entry")
            entry = self.stash[span_id] = RequestStash()
        if request_headers is not None:
            entry.request_headers = request_headers
        if request_body is not None:
            entry.request_body = request_body
        if response_headers is not None:
            entry.response_headers = response_headers
        if response_body is not None:
            entry.response_body = response_body

    def process_ended_span(self, span: ReadableSpan, context: SpanContext) -> None:
        keep, server_span_id = self.spans.pop(context.span_id, (False, None))
        if not keep:
            return
        buffer = self.pending.pop(context.span_id, None)
        if buffer is not None:
            # Pending SERVER root: the response-stage decision flushes or discards the whole request
            stash = self.stash.pop(context.span_id, None)
            response_kept = self.sample_response(span, context.trace_id)
            if response_kept:
                for buffered_span in buffer:
                    self.downstream.on_end(buffered_span)
                if stash is not None:
                    # A private copy, because user-attached processors receive the same shared snapshot.
                    # If the batch queue drops the span, the stash is freed with it.
                    span = copy_span_with_attributes(span, dict(span.attributes or {}))
                    setattr(span, STASH_ATTRIBUTE, stash)
                self.downstream.on_end(span)
            else:
                # Mark the request's still-open spans as dropped so telemetry arriving later is discarded
                for span_id, entry in list(self.spans.items()):
                    if entry[1] == context.span_id:
                        self.spans[span_id] = (False, None)
            if self.on_request_finished is not None:
                self.on_request_finished(context.span_id, response_kept)
            return
        pending = self.pending.get(server_span_id) if server_span_id is not None else None
        if pending is not None:
            if len(pending) < MAX_BUFFERED_SPANS:
                pending.append(span)
            else:
                logger.debug("Apitally span buffer cap reached for request, dropping span")
            return
        self.downstream.on_end(span)

    def resolve_server_span_id(self, span_id: int) -> int | None:
        """Return the SERVER span id for an in-flight span, or None if the request is dropped."""
        entry = self.spans.get(span_id)
        return entry[1] if entry else None

    def shutdown(self) -> None:
        # Held spans only miss late attributes; export them as they are
        for span_id in list(self.held):
            self.finish_export(span_id)
        self.deferred.clear()
        # Pending requests' SERVER spans can never export after shutdown, so their telemetry is unreachable
        self.pending.clear()
        self.stash.clear()
        self.downstream.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.downstream.force_flush(timeout_millis)

    def exclude_request(self, span: Span) -> bool:
        attributes = span.attributes or {}
        method = attributes.get("http.request.method") or attributes.get("http.method")
        if method == "OPTIONS":
            return True
        path = attributes.get("url.path") or attributes.get("http.target")
        if path and self.should_exclude_path(str(path).partition("?")[0]):
            return True
        user_agent = attributes.get("user_agent.original") or attributes.get("http.user_agent")
        if user_agent and self.should_exclude_user_agent(str(user_agent)):
            return True
        return False

    @lru_cache(maxsize=1024)
    def should_exclude_path(self, path: str) -> bool:
        return DEFAULT_EXCLUDE_PATH_PATTERN.search(path) is not None or matches_any(self.exclude_path_patterns, path)

    @lru_cache(maxsize=1024)
    def should_exclude_user_agent(self, user_agent: str) -> bool:
        return EXCLUDE_USER_AGENT_PATTERN.search(user_agent) is not None

    def sample_request(self, span: Span, trace_id: int) -> bool:
        rate = self.resolve_sample_rate(self.config.sample_on_request, span, "sample_on_request")
        if rate is None:
            return is_sampled_in(trace_id, self.sample_rate_bound)
        return is_sampled_in(trace_id, TraceIdRatioBased.get_bound_for_rate(rate))

    def sample_response(self, span: ReadableSpan, trace_id: int) -> bool:
        # If no callback is configured, or it returns None, the request-stage decision stands
        rate = self.resolve_sample_rate(self.config.sample_on_response, span, "sample_on_response")
        return rate is None or is_sampled_in(trace_id, TraceIdRatioBased.get_bound_for_rate(rate))

    def resolve_sample_rate(
        self, callback: Callable[[ReadableSpan], float | bool | None] | None, span: ReadableSpan, name: str
    ) -> float | None:
        """Map a sampling callback result to an effective rate. None means there was no callback, or it returned None."""
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


def copy_span_with_attributes(span: ReadableSpan, attributes: dict[str, AttributeValue]) -> ReadableSpan:
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


def is_contrib_receive_send_span(span: Span) -> bool:
    return (
        span.kind == SpanKind.INTERNAL
        and span.name.endswith((" http send", " http receive", " websocket send", " websocket receive"))
        and span.instrumentation_scope is not None
        and span.instrumentation_scope.name.startswith("opentelemetry.instrumentation.")
    )
