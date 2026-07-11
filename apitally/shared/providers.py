import logging
import uuid
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from typing import cast

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import AggregationTemporality, MetricReader
from opentelemetry.sdk.metrics.view import Aggregation
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, ParentBased, Sampler, TraceIdRatioBased

from apitally.shared.config import ApitallyConfig, get_config


logger = logging.getLogger(__name__)

MAX_ATTRIBUTE_LENGTH = 65_536

try:
    DISTRO_VERSION = version("apitally")
except PackageNotFoundError:  # pragma: no cover
    DISTRO_VERSION = "unknown"

sampler_warned = False
span_limits_warned = False


def get_user_tracer_provider() -> TracerProvider | None:
    """Return the user's previously configured TracerProvider, or None if Apitally should set up its own."""
    provider = trace.get_tracer_provider()
    if isinstance(provider, trace.ProxyTracerProvider):
        return None
    return cast(TracerProvider, provider)


def resolve_env(user_provider: TracerProvider | None) -> str:
    config = get_config() or ApitallyConfig()
    if user_provider is not None:
        resource_env = user_provider.resource.attributes.get("deployment.environment.name")
        if resource_env:
            if config.env not in (resource_env, ApitallyConfig.env):
                logger.warning(
                    "Configured Apitally env '%s' conflicts with the existing OpenTelemetry resource attribute "
                    "deployment.environment.name='%s'; using '%s'. To resolve this, either remove the env argument "
                    "from init_apitally() or set the deployment.environment.name resource attribute to '%s' in "
                    "your OpenTelemetry setup.",
                    config.env,
                    resource_env,
                    resource_env,
                    config.env,
                )
            return str(resource_env)
    return config.env


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
    # length limit env vars never apply
    provider = TracerProvider(
        sampler=ALWAYS_ON,
        resource=resource,
        span_limits=SpanLimits(
            max_attribute_length=MAX_ATTRIBUTE_LENGTH,
            max_span_attribute_length=MAX_ATTRIBUTE_LENGTH,
        ),
    )
    provider.add_span_processor(span_processor)
    trace.set_tracer_provider(provider)
    return provider


def attach_to_tracer_provider(user_provider: TracerProvider, span_processor: SpanProcessor) -> None:
    warn_if_sampler_drops_spans(user_provider.sampler)
    warn_if_attribute_length_limit_too_low(user_provider)
    user_provider.add_span_processor(span_processor)


def create_meter_provider(resource: Resource, metric_readers: Sequence[MetricReader]) -> MeterProvider:
    # Private instance, never registered via set_meter_provider
    return MeterProvider(metric_readers=metric_readers, resource=resource)


def create_logger_provider(resource: Resource, processors: Sequence[LogRecordProcessor] = ()) -> LoggerProvider:
    # Private instance, never registered via set_logger_provider
    provider = LoggerProvider(resource=resource)
    for processor in processors:
        provider.add_log_record_processor(processor)
    return provider


def create_span_exporter(env: str) -> OTLPSpanExporter:
    return OTLPSpanExporter(endpoint=endpoint_url("/v1/traces"), headers=export_headers(env))


def create_metric_exporter(
    env: str,
    preferred_temporality: dict[type, AggregationTemporality] | None = None,
    preferred_aggregation: dict[type, Aggregation] | None = None,
) -> OTLPMetricExporter:
    return OTLPMetricExporter(
        endpoint=endpoint_url("/v1/metrics"),
        headers=export_headers(env),
        preferred_temporality=preferred_temporality,
        preferred_aggregation=preferred_aggregation,
    )


def create_log_exporter(env: str) -> OTLPLogExporter:
    return OTLPLogExporter(endpoint=endpoint_url("/v1/logs"), headers=export_headers(env))


def reset() -> None:
    global sampler_warned, span_limits_warned
    sampler_warned = False
    span_limits_warned = False


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


def warn_if_attribute_length_limit_too_low(user_provider: TracerProvider) -> None:
    global span_limits_warned
    config = get_config() or ApitallyConfig()
    capture_enabled = (
        config.log_request_headers or config.log_request_body or config.log_response_headers or config.log_response_body
    )
    if not capture_enabled or span_limits_warned:
        return
    limits = getattr(user_provider, "_span_limits", None)
    max_length = getattr(limits, "max_span_attribute_length", None)
    if max_length is not None and max_length < MAX_ATTRIBUTE_LENGTH:
        span_limits_warned = True
        logger.warning(
            "The existing OpenTelemetry tracer provider limits span attribute values to %d characters, so "
            "request and response bodies captured by Apitally may be truncated. Raise the limit to at least "
            "%d, e.g. via the OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT environment variable.",
            max_length,
            MAX_ATTRIBUTE_LENGTH,
        )


def endpoint_url(path: str) -> str:
    config = get_config() or ApitallyConfig()
    return config.otlp_endpoint.rstrip("/") + path


def export_headers(env: str) -> dict[str, str]:
    config = get_config() or ApitallyConfig()
    return {"Authorization": f"Bearer {config.write_token}", "Apitally-Env": env}
