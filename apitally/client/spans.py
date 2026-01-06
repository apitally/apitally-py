import threading
from collections import defaultdict
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator, Optional

from apitally.client.logging import get_logger
from apitally.client.request_logging import SpanDict


try:
    from opentelemetry import context as context_api
    from opentelemetry import trace as trace_api
    from opentelemetry.sdk.trace import ReadableSpan, Span, TracerProvider
    from opentelemetry.sdk.trace.export import SpanProcessor

    OPENTELEMETRY_INSTALLED = True
except ImportError:  # pragma: no cover
    OPENTELEMETRY_INSTALLED = False

if TYPE_CHECKING:
    from opentelemetry import context as context_api
    from opentelemetry import trace as trace_api
    from opentelemetry.sdk.trace import ReadableSpan, Span, TracerProvider
    from opentelemetry.sdk.trace.export import SpanProcessor


logger = get_logger(__name__)

_BaseClass: Any = SpanProcessor if OPENTELEMETRY_INSTALLED else object


class SpanCollector(_BaseClass):
    def __init__(self, enabled: bool = True) -> None:
        if enabled and not OPENTELEMETRY_INSTALLED:
            logger.warning("`capture_traces=True` requires the `opentelemetry-sdk` package")
            enabled = False

        self.enabled = enabled
        self.included_span_ids: dict[int, set[int]] = {}
        self.collected_spans: dict[int, list[SpanDict]] = defaultdict(list)
        self.tracer: Optional[trace_api.Tracer] = None
        self.lock = threading.Lock()

        if enabled:
            self._setup_tracer_provider()

    def _setup_tracer_provider(self) -> None:
        provider = trace_api.get_tracer_provider()
        if not isinstance(provider, TracerProvider):
            provider = TracerProvider()
            trace_api.set_tracer_provider(provider)
        provider.add_span_processor(self)
        self.tracer = provider.get_tracer("apitally")

    @contextmanager
    def collect(self) -> Iterator[Optional[int]]:
        if not self.enabled or self.tracer is None:
            yield None
            return

        with self.tracer.start_as_current_span("handle_request") as span:
            ctx = span.get_span_context()
            with self.lock:
                self.included_span_ids[ctx.trace_id] = {ctx.span_id}
            yield ctx.trace_id

    def on_start(self, span: Span, parent_context: Optional[context_api.Context] = None) -> None:
        ctx = span.get_span_context()
        if ctx is None:
            return  # pragma: no cover

        with self.lock:
            included = self.included_span_ids.get(ctx.trace_id)
            if not included:
                return

            if span.parent is not None and span.parent.span_id in included:
                included.add(ctx.span_id)

    def on_end(self, span: ReadableSpan) -> None:
        ctx = span.get_span_context()
        if ctx is None:
            return  # pragma: no cover

        with self.lock:
            included = self.included_span_ids.get(ctx.trace_id)
            if not included or ctx.span_id not in included:
                return

            data = self.serialize_span(span)
            if data is not None:
                self.collected_spans[ctx.trace_id].append(data)

    def get_and_clear_spans(self, trace_id: int) -> list[SpanDict]:
        """Retrieve all collected spans for the given trace ID and clean up."""
        with self.lock:
            self.included_span_ids.pop(trace_id, None)
            return self.collected_spans.pop(trace_id, [])

    def shutdown(self) -> None:  # pragma: no cover
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # pragma: no cover
        return True

    @staticmethod
    def serialize_span(span: ReadableSpan) -> Optional[SpanDict]:
        """Serialize a span to a dictionary for logging."""
        ctx = span.get_span_context()
        if ctx is None or span.start_time is None or span.end_time is None:
            return None  # pragma: no cover

        data: SpanDict = {
            "span_id": format(ctx.span_id, "016x"),
            "parent_span_id": format(span.parent.span_id, "016x") if span.parent else None,
            "name": span.name,
            "start_time": span.start_time,
            "end_time": span.end_time,
        }
        if span.kind and span.kind != trace_api.SpanKind.INTERNAL:
            data["kind"] = span.kind.name
        if span.status and span.status.status_code != trace_api.StatusCode.UNSET:
            data["status"] = span.status.status_code.name
        if span.attributes:
            data["attributes"] = dict(span.attributes)
        return data
