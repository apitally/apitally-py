from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import TYPE_CHECKING, Any

from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from apitally.shared import config, export, metrics, providers, sentry
from apitally.shared.config import TRUE_VALUES, ApitallyConfig
from apitally.shared.export import ExportWorker
from apitally.shared.exporter import ApitallySpanExporter
from apitally.shared.log_processor import ApitallyLogRecordProcessor, install_root_handler, uninstall_root_handler
from apitally.shared.span_processor import ApitallySpanProcessor
from apitally.shared.spool import Spool


if TYPE_CHECKING:
    from _typeshed.wsgi import StartResponse, WSGIApplication, WSGIEnvironment


logger = logging.getLogger(__name__)

activation_lock = threading.Lock()
activation_attempted = False
activated = False
fork_handlers_registered = False
on_activate_hooks: list[Callable[[], None]] = []

env: str | None = None
resource: Resource | None = None
span_processor: ApitallySpanProcessor | None = None
log_processor: ApitallyLogRecordProcessor | None = None
logger_provider: LoggerProvider | None = None
spool: Spool | None = None
export_worker: ExportWorker | None = None

# OTel's own fork handlers hold weak references to batch processors; keep shut-down
# instances alive so a later fork never calls a dead reference
retired_processors: list[SpanProcessor | LogRecordProcessor] = []

# The forked child inherits the tracer provider with this processor already attached;
# re-activation reuses it instead of attaching a second one
inherited_span_processor: ApitallySpanProcessor | None = None


def configure(**kwargs: Any) -> ApitallyConfig:
    """Records configuration only. Threads and network I/O are deferred to activate()."""
    global fork_handlers_registered
    cfg = config.set_config(**kwargs)
    config.ensure_semconv_opt_in()
    sentry.install()
    if not fork_handlers_registered and hasattr(os, "register_at_fork"):
        fork_handlers_registered = True
        os.register_at_fork(
            before=before_fork, after_in_parent=after_fork_in_parent, after_in_child=after_fork_in_child
        )
    return cfg


def activate() -> None:
    """Activate the telemetry pipelines exactly once."""
    global activation_attempted, activated
    with activation_lock:
        if activation_attempted:
            return
        activation_attempted = True
        if should_skip_activation():
            return
        try:
            start_pipelines()
            activated = True
        except Exception:  # pragma: no cover
            logger.exception("Apitally activation failed")
            return
        for hook in on_activate_hooks:
            try:
                hook()
            except Exception:  # pragma: no cover
                logger.exception("Error in Apitally on-activate hook")


def shutdown() -> None:
    """Final flush and send of all buffered telemetry on graceful shutdown."""
    if export_worker is not None:
        export_worker.shutdown()


def is_activated() -> bool:
    return activated


def register_on_activate_hook(hook: Callable[[], None]) -> None:
    on_activate_hooks.append(hook)


class ASGIActivationShim:
    """Outermost ASGI layer. Activates on lifespan startup completion or on the first request,
    and flushes on lifespan shutdown."""

    def __init__(self, app: Callable[..., Awaitable[Any]]) -> None:
        self.app = app

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] == "lifespan":

            async def send_wrapper(message: MutableMapping[str, Any]) -> None:
                if message["type"] == "lifespan.startup.complete":
                    activate()
                elif message["type"] in ("lifespan.shutdown.complete", "lifespan.shutdown.failed"):
                    shutdown()
                await send(message)

            await self.app(scope, receive, send_wrapper)
            return
        if not activation_attempted:
            activate()
        await self.app(scope, receive, send)


class WSGIActivationShim:
    """Outermost WSGI layer. Activates on the first request."""

    def __init__(self, wsgi_app: WSGIApplication) -> None:
        self.wsgi_app = wsgi_app

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        if not activation_attempted:
            activate()
        return self.wsgi_app(environ, start_response)


def should_skip_activation() -> bool:
    return (
        not config.is_configured()
        or config.get_config().disabled
        or bool(os.environ.get("PYTEST_CURRENT_TEST"))
        or sys.argv[1:2] == ["test"]  # detects Django's "manage.py test"
        or (os.environ.get("APITALLY_DISABLED") or "").strip().lower() in TRUE_VALUES
    )


def create_batch_span_processor(spool: Spool) -> BatchSpanProcessor:
    return BatchSpanProcessor(
        ApitallySpanExporter(export.create_span_exporter(spool)),
        max_queue_size=export.BATCH_MAX_QUEUE_SIZE,
        schedule_delay_millis=export.BATCH_SCHEDULE_DELAY_MILLIS,
        max_export_batch_size=export.BATCH_MAX_EXPORT_BATCH_SIZE,
        export_timeout_millis=export.BATCH_EXPORT_TIMEOUT_MILLIS,
    )


def create_batch_log_processor(spool: Spool) -> BatchLogRecordProcessor:
    return BatchLogRecordProcessor(
        export.create_log_exporter(spool),
        max_queue_size=export.BATCH_MAX_QUEUE_SIZE,
        schedule_delay_millis=export.BATCH_SCHEDULE_DELAY_MILLIS,
        max_export_batch_size=export.BATCH_MAX_EXPORT_BATCH_SIZE,
        export_timeout_millis=export.BATCH_EXPORT_TIMEOUT_MILLIS,
    )


def start_pipelines() -> None:
    global env, resource, span_processor, log_processor, logger_provider, inherited_span_processor
    global spool, export_worker
    user_provider = providers.get_user_tracer_provider()
    env = providers.resolve_env(user_provider)
    resource = providers.create_resource(env)
    metrics.setup(resource)
    spool = Spool()
    if inherited_span_processor is not None:
        # Forked child re-activation: swap in a fresh downstream batch processor, like
        # after_fork_in_parent, and drop the parent's in-flight and pending request state
        span_processor = inherited_span_processor
        inherited_span_processor = None
        span_processor.spans.clear()
        span_processor.pending.clear()
        span_processor.deferred.clear()
        span_processor.held.clear()
        span_processor.stash.clear()
        span_processor.downstream = create_batch_span_processor(spool)
    else:
        span_processor = ApitallySpanProcessor(create_batch_span_processor(spool))
        if user_provider is not None:
            providers.attach_to_tracer_provider(user_provider, span_processor)
        else:
            providers.setup_tracer_provider(resource, span_processor)
    log_processor = ApitallyLogRecordProcessor(create_batch_log_processor(spool), span_processor)
    logger_provider = providers.create_logger_provider(resource, [log_processor])
    install_root_handler(logger_provider, span_processor)
    metrics.attach_reader(spool)
    export_worker = ExportWorker(spool, span_processor, log_processor, env)
    export_worker.start()


def before_fork() -> None:
    """Stop all SDK-owned threads so none are running at the instant of fork."""
    activation_lock.acquire()
    if not activated:  # pragma: no cover
        return
    try:
        if export_worker is not None:
            export_worker.stop()
        metrics.detach_reader()
        if span_processor is not None:
            retired_processors.append(span_processor.downstream)
            span_processor.downstream.shutdown()
        if log_processor is not None:
            retired_processors.append(log_processor.downstream)
            log_processor.downstream.shutdown()
        if spool is not None:
            spool.close_current_files()
    except Exception:  # pragma: no cover
        logger.exception("Error stopping Apitally threads before fork")


def after_fork_in_parent() -> None:
    """Re-activate by swapping fresh batch processors into the registered wrappers."""
    try:
        if not activated or spool is None:  # pragma: no cover
            return
        if span_processor is not None:
            span_processor.downstream = create_batch_span_processor(spool)
        if log_processor is not None:
            log_processor.downstream = create_batch_log_processor(spool)
        metrics.attach_reader(spool)
        if export_worker is not None:
            export_worker.start()
    except Exception:  # pragma: no cover
        logger.exception("Error re-activating Apitally after fork")
    finally:
        activation_lock.release()


def after_fork_in_child() -> None:
    """Reset to configured state; the child activates itself if it ever serves. Abandoning
    the inherited spool is safe: it deletes files only through explicit calls."""
    global activation_lock, activation_attempted, activated, env, resource
    global span_processor, log_processor, logger_provider, inherited_span_processor
    global spool, export_worker
    activation_lock = threading.Lock()
    if not activated:  # pragma: no cover
        return
    try:
        uninstall_root_handler()
        metrics.reset()
    except Exception:  # pragma: no cover
        logger.exception("Error resetting Apitally in forked child")
    activation_attempted = False
    activated = False
    env = None
    resource = None
    inherited_span_processor = span_processor
    span_processor = None
    log_processor = None
    logger_provider = None
    spool = None
    export_worker = None


def reset() -> None:
    """Full teardown for tests. Shuts down and drops all pipeline state."""
    global activation_lock, activation_attempted, activated, env, resource
    global span_processor, log_processor, logger_provider, inherited_span_processor
    global spool, export_worker
    activation_lock = threading.Lock()
    uninstall_root_handler()
    metrics.reset()
    if export_worker is not None:
        export_worker.stop(timeout=1.0)
    if span_processor is not None:
        span_processor.downstream.shutdown()
    if log_processor is not None:
        log_processor.downstream.shutdown()
    if spool is not None:
        spool.clear()
    activation_attempted = False
    activated = False
    env = None
    resource = None
    span_processor = None
    log_processor = None
    logger_provider = None
    inherited_span_processor = None
    spool = None
    export_worker = None
    on_activate_hooks.clear()
