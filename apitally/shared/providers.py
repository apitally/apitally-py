import logging
import uuid
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from typing import Protocol, cast

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    Sampler,
    SamplingResult,
    TraceIdRatioBased,
)
from opentelemetry.trace import Link, SpanKind, TraceState
from opentelemetry.util.types import Attributes

from apitally.shared.config import get_config
from apitally.shared.context import reset_server_span


logger = logging.getLogger(__name__)

MAX_ATTRIBUTE_LENGTH = 65_536

try:
    DISTRO_VERSION = version("apitally")
except PackageNotFoundError:  # pragma: no cover
    DISTRO_VERSION = "unknown"

sampler_warned = False


class TracerProviderWithSpanProcessors(Protocol):
    def add_span_processor(self, span_processor: SpanProcessor) -> None: ...


class RequestSampler(Sampler):
    """Applies the request-stage sample_rate test to SERVER spans and records all other spans.
    Wrapped in ParentBased, so it only decides for root spans and spans with an unsampled remote parent.
    A sampled remote parent is always recorded so the upstream trace stays complete in downstream services."""

    def __init__(self, rate: float) -> None:
        self.ratio_sampler = TraceIdRatioBased(rate)

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        sampler = self.ratio_sampler if kind == SpanKind.SERVER else ALWAYS_ON
        result = sampler.should_sample(parent_context, trace_id, name, kind=kind, attributes=attributes, links=links)
        if not result.decision.is_recording():
            # The span processor never sees a non-recording span, so clear the request handles that a
            # reused context (e.g. a WSGI worker thread) still holds from its previous request
            reset_server_span()
        return result

    def get_description(self) -> str:
        return f"RequestSampler{{{self.ratio_sampler.rate}}}"


def get_user_tracer_provider() -> TracerProviderWithSpanProcessors | None:
    """Return the user's previously configured tracer provider, or None if Apitally should set up its own."""
    provider = trace.get_tracer_provider()
    if isinstance(provider, trace.ProxyTracerProvider):
        return None
    if not callable(getattr(provider, "add_span_processor", None)):
        raise TypeError(
            f"The registered OpenTelemetry tracer provider ({type(provider).__qualname__}) does not expose "
            f"the add_span_processor interface required by Apitally"
        )
    return cast(TracerProviderWithSpanProcessors, provider)


def create_resource(env: str) -> Resource:
    # Resource.create picks up OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES; the Apitally-required
    # attributes are merged on top so the Apitally-Env header always matches the resource
    return Resource.create({}).merge(
        Resource(
            {
                "service.instance.id": str(uuid.uuid4()),
                "deployment.environment.name": env,
                "telemetry.distro.name": "apitally-py",
                "telemetry.distro.version": DISTRO_VERSION,
            }
        )
    )


def setup_tracer_provider(resource: Resource, span_processor: SpanProcessor) -> TracerProvider:
    # Sampler and limits are passed explicitly so OTEL_TRACES_SAMPLER and the attribute
    # length limit env vars never apply. A sample_on_request callback can raise the rate above
    # sample_rate, so with a callback every request is recorded and the span processor decides.
    config = get_config()
    request_sampler = RequestSampler(1.0 if config.sample_on_request is not None else config.sample_rate)
    provider = TracerProvider(
        sampler=ParentBased(root=request_sampler, remote_parent_not_sampled=request_sampler),
        resource=resource,
        span_limits=SpanLimits(
            max_attribute_length=MAX_ATTRIBUTE_LENGTH,
            max_span_attribute_length=MAX_ATTRIBUTE_LENGTH,
        ),
    )
    provider.add_span_processor(span_processor)
    trace.set_tracer_provider(provider)
    return provider


def attach_to_tracer_provider(user_provider: TracerProviderWithSpanProcessors, span_processor: SpanProcessor) -> None:
    sampler = getattr(user_provider, "sampler", None)
    if sampler is not None:
        warn_if_sampler_drops_spans(sampler)
    user_provider.add_span_processor(span_processor)


def create_logger_provider(resource: Resource, processors: Sequence[LogRecordProcessor] = ()) -> LoggerProvider:
    # Private instance, never registered via set_logger_provider
    provider = LoggerProvider(resource=resource)
    for processor in processors:
        provider.add_log_record_processor(processor)
    return provider


def reset() -> None:
    global sampler_warned
    sampler_warned = False


def warn_if_sampler_drops_spans(sampler: Sampler) -> None:
    global sampler_warned
    root = sampler._root if isinstance(sampler, ParentBased) else sampler
    if root is ALWAYS_OFF or isinstance(root, TraceIdRatioBased):
        if not sampler_warned:
            sampler_warned = True
            logger.warning(
                "The existing OpenTelemetry tracer provider uses a sampler (%s) that drops spans, so Apitally "
                "will not capture request logs for sampled-out requests. To get full coverage, raise the "
                "sampling rate or initialize Apitally before your OpenTelemetry setup so it manages its own "
                "tracer provider.",
                sampler.get_description(),
            )


def endpoint_url(path: str) -> str:
    config = get_config()
    return config.otlp_endpoint.rstrip("/") + path


def export_headers(env: str) -> dict[str, str]:
    config = get_config()
    return {"Authorization": f"Bearer {config.write_token}", "Apitally-Env": env}
