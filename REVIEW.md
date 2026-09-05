# Code review: apitally-py v1 (branch `v1`, commit `3022e44`)

Scope: every module under `apitally/`, the tests, and the installed sources of the OTel SDK, the contrib instrumentations, Django, Starlette/FastAPI, Flask, Litestar and BlackSheep where the SDK's behaviour depends on them. Four reviewers covered the core pipeline, the ASGI/WSGI transports and framework adapters, the Django/OTel/Sentry integrations, and cross-cutting lifecycle and concurrency. Every finding below was then independently reproduced with a script against the installed versions (Django 6.0.6, FastAPI 0.139, Starlette 1.3.1, Flask 3.1.3, OTel SDK 1.43, contrib 0.64b0) or verified line by line in the code. Findings that could not be substantiated, or that need an unrealistic combination of conditions, were rejected; the notable rejections are listed at the end.

Severity: **High** = data loss, silent breakage of a core feature, or unbounded resource growth in a normal deployment. **Medium** = wrong behaviour or a real operational problem in a common configuration. **Low** = real but narrow, or cosmetic/perf.

Each finding carries a **Status** line (Open or Fixed, with what was done) so this file doubles as the tracker for what has been actioned.

---

## High

### H1. The metrics SDK retains one aggregation per distinct consumer forever (memory leak in every worker)

**Status:** Fixed. `ApitallyMetricReader.collect` now calls `drop_histogram_aggregations`, which clears the histogram aggregation dicts after every collection. A measurement that races with the clear can be lost, which is acceptable for DELTA request metrics. Covered by `test_collect_appends_delta_payloads_to_spool`.

**Where:** [metrics.py:96-111](apitally/shared/metrics.py:96)

`record_request` records into three histograms with `apitally.consumer.identifier` as an attribute. The OTel Python SDK keeps a `_attributes_aggregation: dict[frozenset, _Aggregation]` per instrument and never evicts entries; DELTA temporality resets the buckets on `collect()` but keeps the aggregation object, its lock and the frozenset key. The Python SDK has no cardinality limit.

**Reproduced:** after `metrics.setup`, recording 5,000 distinct consumers and collecting twice leaves 15,303 aggregation entries and **29.5 MB retained (about 5.9 KB per consumer)**.

**Scenario:** the documented pattern is `set_consumer(user.identifier, ...)`. A multi-tenant API whose workers see 100k distinct users over their lifetime retains on the order of 600 MB per worker process, growing monotonically until the worker is recycled. Likelihood: certain for any deployment with per-user consumers, which is the primary use of the consumer feature.

**Fix:** since temporality is DELTA, nothing in an aggregation is worth keeping after collection. In `ApitallyMetricReader.collect`, after `super().collect(...)`, clear each `_ViewInstrumentMatch._attributes_aggregation` under its lock (private API, so pin it with a test), or rebuild the histogram instruments per cycle. Alternatively cap consumer cardinality per cycle in `record_request`.

### H2. Django settings packages (`from .base import *`) silently lose the OTel middleware

**Status:** Fixed. `_insert_middleware` inserts the OTel middleware into the caller's list itself when missing, then Apitally right after it, instead of relying on the instrumentor's write to `settings`. Verified against a split-settings project outside the repo; the existing `test_init_from_settings_module` covers the single-module layout only.

**Where:** [django.py:85-88](apitally/django.py:85), [django.py:101-113](apitally/django.py:101)

`_insert_middleware` assumes `DjangoInstrumentor.instrument()` inserted `_DjangoMiddleware` into the caller's `MIDDLEWARE` list. The instrumentor does `getattr(settings, "MIDDLEWARE")`, which triggers `LazySettings._setup()` while the settings module is still being imported. When Django itself imports settings (the normal path for `manage.py`, `gunicorn proj.wsgi`, `django.setup()`), this is a nested `Settings()` that re-imports the partially initialised module named by `DJANGO_SETTINGS_MODULE`. If `apitally.init()` sits in `settings/base.py` and `DJANGO_SETTINGS_MODULE=proj.settings.production` does `from .base import *`, the partial `production` module has no `MIDDLEWARE` yet, so the instrumentor inserts into `django.conf.global_settings.MIDDLEWARE` instead. Apitally then does not find `OTEL_MIDDLEWARE` in the caller's list and inserts itself at index 0.

**Reproduced** (settings package in the scratchpad):

```
DJANGO_SETTINGS_MODULE=proj.settings.production, init at end of base.py
settings.MIDDLEWARE = ['apitally.django.ApitallyDjangoMiddleware', 'django.middleware.common.CommonMiddleware']
global_settings.MIDDLEWARE = ['opentelemetry...._DjangoMiddleware']   # polluted default
```

With a single settings module the nested import already has `MIDDLEWARE` defined and everything works, which is the only layout the tests cover (`tests/test_django.py`, `tests/django/settings.py`).

**Scenario:** the base/dev/prod settings package is the most common Django layout (cookiecutter-django and most mid-size projects). "Call at the end of settings.py" is ambiguous there and `base.py` is the natural place. Likelihood: medium-high. **Impact:** silent. Metrics still flow because `ApitallyDjangoMiddleware` runs and `resolver_match` is set, but there is no SERVER span, so no traces, no request logs, no header/body capture, and validation/server error events are the only extra signal.

**Fix:** stop depending on the instrumentor's write to `settings`. Keep calling `instrument()` (it sets `_DjangoMiddleware._tracer` etc.), then insert both entries into the caller's list yourself: `if OTEL_MIDDLEWARE not in middleware: middleware.insert(0, OTEL_MIDDLEWARE)`, then Apitally right after it. Add a split-settings test.

### H3. Installing the root `LoggingHandler` silences Python's `lastResort` output and Flask's default handler

**Status:** Fixed. `ApitallyLoggingHandler.handle` forwards records at or above the `lastResort` level to `logging.lastResort` when no other handler exists in the logger's chain to root; the loguru sink bypasses this since loguru writes to its own sinks. Flask `init` touches `app.logger` before activation so Flask still installs its default handler. Covered by `test_last_resort_output_kept_for_loggers_without_other_handlers` and `test_flask_default_log_handler_survives_activation`.

**Where:** [log_processor.py:40-43](apitally/shared/log_processor.py:40)

`Logger.callHandlers` only falls back to `logging.lastResort` (WARNING+ to stderr) when it finds zero handlers in the hierarchy. A NOTSET handler on the root counts, so every logger in the process loses its fallback output. Module-level `logging.warning(...)` also stops calling `basicConfig()` implicitly. Flask's `has_level_handler` walks to root and returns True for Apitally's handler, so `app.logger` never gets Flask's `default_handler` when first touched after activation, and Flask's own `Exception on /path [GET]` tracebacks stop reaching stderr.

**Reproduced:** `logging.getLogger("myapp").warning(...)` prints to stderr before the handler is added and prints nothing after. A Flask app created after the handler exists has `app.logger.handlers == []` and `app.logger.error(...)` produces no stderr output.

**Scenario:** the default deployment. gunicorn and uvicorn configure only their own named loggers and leave the root handler-less; small FastAPI/Flask services that never call `basicConfig`/`dictConfig` rely on `lastResort`. `capture_logs=True` is the default, activation happens on first request or lifespan startup, and from then on warnings and exception tracebacks vanish from the console/log aggregator with no indication why. The captured copies only reach Apitally for kept requests inside a SERVER span, so startup and background warnings are lost entirely. Likelihood: high for apps without explicit logging config. Sentry's logging integration has the same side effect, so users may already tolerate it, but it deserves at least a fix for the fallback.

**Fix:** preserve `lastResort` semantics in the Apitally handler: when the root has no other handlers and `record.levelno >= logging.lastResort.level`, forward the record to `logging.lastResort` before applying the request filters. For Flask, touch `app.logger` in `apitally/flask.py:init` before wrapping so Flask installs its default handler first. Add a `capsys` test.

---

## Medium

### M1. `metrics.setup` hijacks an existing `SystemMetricsInstrumentor` and breaks the user's metrics pipeline

**Status:** Open.

**Where:** [metrics.py:74-77](apitally/shared/metrics.py:74), [metrics.py:118-120](apitally/shared/metrics.py:118)

`SystemMetricsInstrumentor` is a process singleton. `SystemMetricsInstrumentor(config=SYSTEM_METRICS_CONFIG)` re-runs `__init__` on the existing instance and overwrites its `_config`; `uninstrument()` then does not (cannot) remove the observable instruments already registered on the user's `MeterProvider`, whose callbacks are bound to the same singleton.

**Reproduced:** user instruments with their own `MeterProvider` (30 metrics collected). After `metrics.setup`, the user's reader collects 12 metrics, and every collection logs a `Callback failed for instrument system.cpu.time ... KeyError: 'system.cpu.time'` traceback for each of the ~18 metrics whose callbacks now index a config that lacks them. Conversely, if Apitally activates first, a later user `SystemMetricsInstrumentor().instrument(meter_provider=...)` is refused by the singleton guard and the user gets no system metrics at all.

**Scenario:** anyone running under `opentelemetry-instrument` auto-instrumentation, which instruments every installed instrumentor, and Apitally itself installs `opentelemetry-instrumentation-system-metrics` as a hard dependency. Also anyone with a manual OTel metrics setup that includes system metrics. This is exactly the "alongside an existing OpenTelemetry setup" audience MIGRATION.md addresses. Likelihood: medium. **Impact:** the user's system metrics break with log spam at every interval; `metrics.reset()` on fork repeats the dance.

**Fix:** do not use the shared singleton. Read the two process gauges directly via `psutil` with your own observable-gauge callbacks on the private meter (about 15 lines), and drop the dependency on the system-metrics instrumentor. At minimum, skip the takeover when `is_instrumented_by_opentelemetry` is already True and log why.

### M2. Every stdlib log record is captured twice with the standard loguru `InterceptHandler` recipe

**Status:** Open.

**Where:** [log_processor.py:41-45](apitally/shared/log_processor.py:41), [log_processor.py:57-88](apitally/shared/log_processor.py:57)

The root `LoggingHandler` captures stdlib records, and the loguru sink (installed whenever loguru is importable) captures loguru messages. The documented loguru integration for stdlib is an `InterceptHandler` on the root logger that forwards stdlib records into loguru. With both in place, one `logging.getLogger("myapp").warning(...)` goes through Apitally's root handler and through InterceptHandler, then loguru, then Apitally's sink.

**Reproduced:** one stdlib warning inside a kept request produces two identical log records in the private LoggerProvider.

**Scenario:** the InterceptHandler recipe is the canonical way FastAPI+loguru apps unify logging (it is the first thing in loguru's "entirely compatible with standard logging" docs). Likelihood: high among loguru users. **Impact:** doubled log volume and quota for every stdlib record, duplicated lines in the Apitally UI.

**Fix:** in the loguru sink, skip records whose `record["extra"]` or origin indicates they came from stdlib via InterceptHandler is not reliable; instead mark records the root handler already saw (for example, in the sink check whether the current stack passed through `logging.Handler.handle` of the Apitally handler, or simpler: when the root logger has a handler that is not Apitally's and loguru's `record["name"]` resolves to a stdlib logger, skip). The cleanest option is a documented setting: only install the loguru sink when loguru is actually configured with sinks of its own, and document that InterceptHandler users should set `capture_logs` at one layer. Whatever the mechanism, add a test with the InterceptHandler recipe asserting one record.

### M3. `ApitallyDjangoMiddleware` is not `async_capable`, forcing the whole outer middleware chain into sync mode under ASGI

**Status:** Rejected after attempting the fix. The premise below is wrong for the installed contrib version (0.64b0, unchanged on upstream main): the OTel `_DjangoMiddleware` is a plain sync-only class without `async_capable`, so Django wraps it in `SyncToAsync` regardless. Making `ApitallyDjangoMiddleware` dual-mode only moves the `AsyncToSync` boundary from below Apitally to between OTel and Apitally; the request still makes exactly one thread round trip. Since Apitally sits directly after OTel, the two form the single contiguous sync block Django's `load_middleware` is designed to adapt once (see Django ticket 37177). Revisit only if the OTel Django middleware gains native async support upstream.

**Where:** [django.py:121-149](apitally/django.py:121)

Django's `load_middleware` builds the chain from the inside out; once it meets a sync-only middleware it runs that middleware and every middleware outside it (and any user middleware placed before Apitally) in sync mode, adapting the top of the stack with `SyncToAsync` and the handler below Apitally with `AsyncToSync`.

**Reproduced:** with `ASGIHandler()` and the default insertion (OTel, Apitally, Common), `_middleware_chain` is a `SyncToAsync` object. Every request therefore hops event loop -> thread (OTel + Apitally, sync) -> event loop (inner async middleware and view). Django logs "Asynchronous handler adapted for middleware apitally.django.ApitallyDjangoMiddleware" at DEBUG.

**Scenario:** any Django project served by uvicorn/daphne with async views, which the `django[asgi]` extra in `pyproject.toml` explicitly targets. Likelihood: high for ASGI Django users. **Impact:** two thread switches per request. Django's docs call this out as a performance penalty to avoid.

**Fix:** implement the dual-mode middleware pattern (`sync_capable = True`, `async_capable = True`, `markcoroutinefunction(self)` when `get_response` is a coroutine function, and an `__acall__` path that awaits `get_response`). `finalize` and the streaming wrappers are already sync-safe helpers, so the change is mostly the call shape.

### M4. Response wrapping defeats `sendfile` for Flask `send_file` and Django `FileResponse`

**Status:** Open.

**Where:** [wsgi.py:73](apitally/shared/wsgi.py:73), [wsgi.py:185-223](apitally/shared/wsgi.py:185); [django.py:332](apitally/django.py:332), [django.py:352](apitally/django.py:352)

WSGI servers (gunicorn sync, uWSGI, waitress, mod_wsgi) take the `sendfile()` path when the app iterable is an instance of `environ["wsgi.file_wrapper"]`. Flask's `send_file` returns exactly that object (`direct_passthrough`), and Django's `WSGIHandler` hands `response.file_to_stream` to the file wrapper. `ResponseWrapper` wraps every iterable unconditionally, and Django's `FileResponse._set_streaming_content` sets `file_to_stream = None` as soon as `streaming_content` is replaced by a generator.

**Reproduced:** Flask: plain app iterable is `FileWrapper`, with Apitally it is `ResponseWrapper`. Django: plain `WSGIHandler` returns `wsgiref.util.FileWrapper`, with Apitally it returns the `FileResponse` itself (chunk iteration in Python). The OTel Flask instrumentor deliberately returns the iterable untouched, so the regression is Apitally's alone, and 0.x did not replace streaming content either.

**Scenario:** any app serving downloads, media or exports. Likelihood: high for those apps. **Impact:** performance only; files are pushed through Python in small chunks instead of kernel `sendfile`, and the body is iterated through the Apitally wrapper.

**Fix:** WSGI: when `isinstance(response, environ.get("wsgi.file_wrapper", ()))`, do not wrap; call `finalize` immediately with `completed = True` and take the size from `Content-Length`. Django: when `getattr(response, "file_to_stream", None) is not None`, skip `finalize_streaming`, record metrics with the `Content-Length` that `FileResponse.set_headers` already set, and let the span export normally. Body capture is irrelevant in both cases because file content types are never in the allowlist.

### M5. Endpoint enumeration and OpenAPI generation run inside the first request, under `activation_lock`

**Status:** Open.

**Where:** [activation.py:79-97](apitally/shared/activation.py:79), [startup.py:48-67](apitally/shared/startup.py:48), [django.py:116-118](apitally/django.py:116), [activation.py:171-174](apitally/shared/activation.py:171)

For WSGI frameworks and Django, `activate()` runs in the first request (`WSGIActivationShim`, `request_started`). It calls the on-activate hooks synchronously while holding `activation_lock`, and `emit_startup_event` resolves the paths and the OpenAPI schema: DRF `EndpointEnumerator` plus a full DRF or drf-spectacular `SchemaGenerator().get_schema()`, or Ninja `get_openapi_schema()` twice (once for paths, once for the schema), then `json.dumps` of up to 4 MB. In 0.x this work ran in the middleware constructor at `load_middleware()` time, off the request path.

**Scenario:** every cold worker's first request, always. On a few-hundred-endpoint drf-spectacular project schema generation takes seconds; concurrent first requests in threaded workers queue on `activation_lock`, and under ASGI the sync `request_started` receiver runs on asgiref's single thread-sensitive executor and blocks other requests' sync middleware too. Likelihood: certain; magnitude scales with API size. **Impact:** multi-second latency on the first request(s) per worker, load-balancer health checks can time out on rollouts.

**Fix:** never run user-visible work inside `activation_lock`. Emit the startup event from the export worker thread on its first cycle (the payload is only needed when the first spool file ships), or start a one-shot thread from `activate()`. Also compute the Ninja schema once and derive paths from it.

### M6. Export failures are invisible at default log level, and `trust_env=False` drops `REQUESTS_CA_BUNDLE`

**Status:** Open.

**Where:** [export.py:101-105](apitally/shared/export.py:101), [export.py:212-214](apitally/shared/export.py:212), [activation.py:64-65](apitally/shared/activation.py:64)

`trust_env=False` disables more than proxy lookup: `requests` only honours `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE` when `trust_env` is True. Proxies were carried over manually via `resolve_proxy_urls()` at configure time, the CA bundle was not. `SSLError` subclasses `ConnectionError`, so certificate failures take the "will retry" path and are logged at DEBUG every cycle, until each file expires after 59 minutes with a WARNING that does not mention TLS.

**Reproduced:** with `REQUESTS_CA_BUNDLE=/etc/ssl/corp.pem`, `merge_environment_settings` yields `verify=True` for the SDK's session and the bundle path with `trust_env=True`.

**Scenario:** enterprise networks with a TLS-intercepting egress proxy, where the standard operator fix is `HTTPS_PROXY` plus `REQUESTS_CA_BUNDLE`. The proxy half works, the CA half is ignored, and there is no error at default log levels. The same silence applies to any permanent connectivity failure (DNS, egress firewall). Likelihood: medium for the CA bundle; the silent-failure surface applies to every misconfigured deployment. **Impact:** zero data reaching Apitally with nothing in the application logs pointing at the cause.

**Fix:** resolve `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE` in `configure()` next to the proxies and set `session.verify`. Log one WARNING with the exception text on the first failure of a streak (reset on the next success), mirroring the existing `warned_statuses` pattern.

### M7. `env` default disagrees between code and migration guide

**Status:** Open.

**Where:** [config.py:37](apitally/shared/config.py:37) (`env: str = "dev"`), [__init__.py:63-64](apitally/__init__.py:63) ("falling back to `dev`"), [MIGRATION.md:38](MIGRATION.md:38) ("default changed to `prod`")

A user following the migration guide and omitting `env` files production traffic under `dev`, or the reverse. `providers.resolve_env` also uses `ApitallyConfig.env` as the "unset" sentinel, so whichever default is intended, the three places must agree. Likelihood: certain for anyone reading the guide.

### M8. `apitally.init()` inside a FastAPI/Starlette lifespan or startup handler is a silent no-op

**Status:** Open.

**Where:** [fastapi.py:63-77](apitally/fastapi.py:63), [starlette.py:82-103](apitally/starlette.py:82)

Both `_instrument_app` functions replace `app.build_middleware_stack` but never rebuild an already-built stack. Starlette builds `middleware_stack` on the first `__call__`, which is the lifespan scope, so an `init()` inside `lifespan=` or `@app.on_event("startup")` runs after the stack exists and the replacement never executes. The pre-instrumented Starlette branch already handles this (`starlette.py:75-76`).

**Reproduced:** `middleware_stack` stays `ServerErrorMiddleware`, `activation.is_activated()` is False, no spans, no log line.

**Scenario:** fetching the write token from a secret manager or async config source in the lifespan handler. 0.x's `add_middleware` raised `RuntimeError("Cannot add middleware after an application has started")`; 1.x says nothing. Likelihood: low-medium. **Impact:** no telemetry at all, silently.

**Fix:** after assigning `build_with_shim`, `if app.middleware_stack is not None: app.middleware_stack = app.build_middleware_stack()`, as in the other branch.

### M9. Shutdown has no overall time budget

**Status:** Open.

**Where:** [export.py:135-141](apitally/shared/export.py:135), [export.py:205-211](apitally/shared/export.py:205)

`shutdown()` joins the worker with no timeout and then runs an uncapped final drain. `send_file` uses `timeout=10` (connect and read separately) and retries once on any `ConnectionError`, which includes `ConnectTimeout`. Against a black-holed host (firewall DROP), one probe costs 20 s; the join may wait on a probe in flight (20 s) and the final cycle runs another (20 s). This runs inside the ASGI `lifespan.shutdown.complete` send, blocking the event loop, and at `atexit`.

**Scenario:** the endpoint is unreachable at shutdown, which is the case for every pod exit during an outage or a misconfigured egress. Kubernetes' default 30 s grace period is exceeded and the pod is SIGKILLed, taking the spool files in the container's `/tmp` with it. Likelihood: low in steady state, certain during outages. **Impact:** slow shutdowns, forced kills, lost buffered data.

**Fix:** bound the join, give the final drain a deadline (single attempt, shorter timeout, skip when the last cycle failed), and restrict the immediate retry to the keep-alive case (a `ConnectionError` that is not a `ConnectTimeout`).

---

## Low

### L1. Response-stage drop scans every in-flight span in the process

**Status:** Open.

**Where:** [span_processor.py:221-224](apitally/shared/span_processor.py:221)

`self.spans` holds every open span of every in-flight request. Each `sample_on_response` drop copies and scans the whole dict. The documented pattern ("capture 5% of healthy responses") drops 95% of requests.

**Measured:** 6 us per dropped request at 200 in-flight spans, 70 us at 2,000. At 5k rps with a few hundred concurrent requests this is tens of percent of a core on the request path. There is also a small race in threaded WSGI: a child span ended by another thread between the `list(...)` snapshot and the assignment gets its popped entry re-inserted as `(False, None)` and never removed.

**Fix:** keep a per-SERVER-span index of descendants populated in `on_start` and pop it in `process_ended_span`; mark only those.

### L2. Query-string redaction rewrites values that were not redacted

**Status:** Fixed. `redact_query_params` now splits the original query on `&` and splices `[REDACTED]` into matched pairs only, so untouched queries are returned byte for byte and no span copy is made for them. Covered by the updated `test_redaction.py` cases.

**Where:** [redaction.py:57-59](apitally/shared/redaction.py:57), [exporter.py:62-74](apitally/shared/exporter.py:62)

The `parse_qsl` -> `urlencode` round trip is not identity: `q=hello%20world` becomes `q=hello+world`, `ids=1,2,3` becomes `ids=1%2C2%2C3`, `flag` becomes `flag=`. Captured `url.query`/`http.target`/`url.full` therefore differ from what the client sent even when nothing matched, and `redacted != value` forces a span copy for every span carrying such a query. Likelihood: every request with commas or encoded spaces. **Impact:** request logs show a rewritten query; wasted copy.

**Fix:** return the original string when no parameter name matches; when one does, splice `[REDACTED]` into the original per matched pair instead of re-encoding.

### L3. Starlette route resolution can return a sibling mount's template

**Status:** Fixed. `_resolve_route` skips routes whose `endpoint` is not `scope["endpoint"]` once routing has set it, so sibling mounts' routes no longer match against the stripped path; `matches()` still decides among routes sharing an endpoint and at span start before routing. Covered by `test_mounted_route_includes_mount_prefix` with a parameterised sibling mount ahead of `/admin`.

**Where:** [starlette.py:132-148](apitally/starlette.py:132)

`_resolve_route` recurses into every Mount's children without checking that the Mount matched (necessary at `finish()` time because Starlette has already extended `root_path`), so children of unrelated mounts are matched against the stripped path.

**Reproduced:** `Mount("/admin", [Route("/{x}")])` before `Mount("/api", [Route("/users")])`: `GET /api/users` resolves to `/{x}`, so the span and the metrics key become `/api/{x}`. Likelihood: low-medium (needs a parameterised route in an earlier sibling mount; admin/API splits make it plausible). **Impact:** wrong aggregation key.

**Fix:** Starlette puts the matched endpoint into `scope["endpoint"]`; select the route whose `endpoint is scope["endpoint"]` and only fall back to `matches()` when that is ambiguous. This also removes the second full `matches()` pass per request.

### L4. Django OpenAPI generation is fragile and fails loudly

**Status:** Open.

**Where:** [django.py:405-410](apitally/django.py:405), [django.py:416-418](apitally/django.py:416), [django_rest_framework.py:23-34](apitally/django_rest_framework.py:23)

Two independent failure paths, each of which drops the OpenAPI payload and logs an ERROR traceback at every startup (which `capture_logs=True` also ships):

- drf-spectacular is detected by the exact string `"drf_spectacular.openapi.AutoSchema"`. Subclassing `AutoSchema` is a documented extension point; with a subclass, DRF's own `SchemaGenerator` is used and calls `view.schema.get_operation(path, method)` against spectacular's `get_operation(path, path_regex, path_prefix, method, registry)`, raising `TypeError`, which `_get_drf_schema` does not suppress.
- DRF's `AutoSchema.map_field` emits `schema['default'] = field.default` verbatim (`rest_framework/schemas/openapi.py:536`). A `DecimalField(default=Decimal("0"))`, `UUID`, `date` or enum default makes `json.dumps` raise `TypeError`; `_convert_proxy_objects` only handles lazy strings.

**Fix:** detect spectacular via `issubclass(import_string(schema_class), drf_spectacular.openapi.AutoSchema)` or `"drf_spectacular" in INSTALLED_APPS`. Replace `_convert_proxy_objects` and the `ProxyValue` type with `json.dumps(schema, cls=rest_framework.utils.encoders.JSONEncoder)` for DRF and `cls=ninja.responses.NinjaJSONEncoder` for Ninja, which handle both lazy strings and these types and remove about 25 lines.

### L5. `requests` exceptions other than connection/timeout are treated as spool read errors and the file is deleted

**Status:** Open.

**Where:** [export.py:212-218](apitally/shared/export.py:212)

`requests.RequestException` subclasses `OSError`, and `MissingSchema`/`InvalidURL`/`InvalidProxyURL` also subclass `ValueError`, so they land in the `except (OSError, ValueError)` clause, which logs "Error reading buffered traces, dropping it" with a traceback and deletes the file. `resolve_config` accepts `APITALLY_OTLP_ENDPOINT=otlp.example.com` without a scheme, and a malformed `HTTPS_PROXY` gives `InvalidProxyURL`.

**Scenario:** a misconfigured endpoint or proxy URL drops every file every cycle with a misleading disk error. Likelihood: low (configuration mistake), but the message points at the wrong subsystem. **Fix:** catch `requests.RequestException` in its own clause before `(OSError, ValueError)`, and validate the endpoint scheme in `resolve_config` the way the token format is validated.

### L6. A non-SDK global `TracerProvider` aborts activation with a traceback

**Status:** Open.

**Where:** [providers.py:31-36](apitally/shared/providers.py:31), [providers.py:43](apitally/shared/providers.py:43)

Anything other than `ProxyTracerProvider` is cast to the SDK `TracerProvider`. `trace.set_tracer_provider(trace.NoOpTracerProvider())` (a user disabling tracing explicitly) or ddtrace's OTel shim provider raises `AttributeError: 'NoOpTracerProvider' object has no attribute 'resource'` inside `start_pipelines`, logged as "Apitally activation failed", and Apitally stays off for the process.

**Reproduced** with `NoOpTracerProvider`. Likelihood: low. **Fix:** `isinstance(provider, opentelemetry.sdk.trace.TracerProvider)`; otherwise log a clear warning explaining that the registered provider cannot be attached to.

### L7. `apitally.init(wrapped_app)` detects the framework through the wrapper but passes the wrapper on

**Status:** Open.

**Where:** [__init__.py:140-147](apitally/__init__.py:140), [__init__.py:133-137](apitally/__init__.py:133)

`_detect_framework_package` unwraps `.app` ("Middleware wrappers like OpenTelemetryMiddleware hold the wrapped app"), but `module.init(app, **kwargs)` receives the original wrapper. Only the BlackSheep adapter handles an `OpenTelemetryMiddleware` argument; for FastAPI/Starlette/Flask `_instrument_app(wrapper)` fails on `build_middleware_stack`/`wsgi_app` and is swallowed into "Apitally setup for FastAPI failed". Likelihood: low. **Fix:** restrict the unwrapping to BlackSheep, or pass the unwrapped app for the frameworks that need it, so the failure is a clear `TypeError`.

### L8. Framework-specific `init(app, **kwargs)` silently drops misspelled options

**Status:** Open.

**Where:** [fastapi.py:25-39](apitally/fastapi.py:25), [starlette.py:31-45](apitally/starlette.py:31), [blacksheep.py:23-37](apitally/blacksheep.py:23), [flask.py:24-38](apitally/flask.py:24)

`config.explicit_kwargs` keeps only names in `CONFIG_FIELDS` and discards the rest without a warning. `apitally.init` has explicit keyword parameters so typos raise `TypeError` there, but MIGRATION.md documents the framework-specific functions as public too, and `apitally.fastapi.init(app, write_token=..., capture_log=False)` keeps capturing logs and says nothing. The log-capture opt-out is the one MIGRATION.md flags as a privacy concern. **Fix:** warn on unknown keys in `explicit_kwargs`, or give the four functions the same explicit signature.

### L9. `instrument_sqlalchemy(engine)` silently no-ops on the second engine

**Status:** Open.

**Where:** [otel.py:168-182](apitally/otel.py:168)

The per-engine signature invites one call per engine, but `BaseInstrumentor.instrument()` is singleton-gated and only warns on the second call, so `instrument_sqlalchemy(primary); instrument_sqlalchemy(replica)` instruments only the primary. The instrumentor supports `engines=[...]`. `engine` is also a required positional with no default, unlike every sibling. **Fix:** accept `engine | engines`, default to `None`, pass `engines=` through.

---

## Minor code quality

- [django.py:200-202](apitally/django.py:200): the Django SERVER span keeps OTel's raw name (`GET items/<int:pk>/`) while the ASGI, BlackSheep and Litestar transports call `span.update_name` after rewriting `http.route`. One line for consistency.
- [django.py:366-377](apitally/django.py:366): `_get_paths` has no per-source isolation. Ninja's `get_openapi_schema()` calls `reverse(f"{namespace}:api-root")` against `ROOT_URLCONF`, so with `django_urlconf=["tenant_urls"]` a `NoReverseMatch` from Ninja also discards the DRF and class-based-view paths.
- [flask.py:54](apitally/flask.py:54), [flask.py:65-67](apitally/flask.py:65): `_set_client_address` reads `request.remote_addr`, which is `environ["REMOTE_ADDR"]`, the same key the Flask instrumentor already reads in `before_request` (after `ProxyFix` has rewritten it). The hook only adds value for a custom `Request` subclass overriding `remote_addr`, and a user `before_request` that short-circuits skips it anyway. Remove or comment the reason.
- [export.py:153-177](apitally/shared/export.py:153): the stages of `run_cycle` are not isolated. An exception in `metrics.reader.collect()` skips `rotate_for_export`/`send_pending` for the cycle, and unexpected cycle exceptions are DEBUG only.
- [log_processor.py:136](apitally/shared/log_processor.py:136): `elif record.attributes is not None` is always true because `ReadWriteLogRecord.__post_init__` unconditionally wraps attributes in `BoundedAttributes`. Dead condition.
- [starlette.py:117-120](apitally/starlette.py:117): `_ExceptionRecordingMiddleware` records the exception on `get_server_span()` but sets the status on `trace.get_current_span()`; same span in practice, one accessor reads cleaner.

---

## Investigated and rejected

- **Thread safety of the span processor dicts** (`spans`, `pending`, `stash`, `held`, `deferred`): request threads only touch their own keys, single dict operations are atomic under the GIL, the export thread never touches them, `Spool` is fully locked. Fine, apart from the narrow re-insert window noted in L1.
- **Fork-after-activation** (`before_fork` joining the export thread for up to 5 s, `retired_processors` growing by two per fork, a lost metrics interval): AGENTS.md declares this lifecycle unsupported and the supported pre-fork path (configure in parent, activate in workers) was traced and is clean. Noted, not flagged.
- **Deferred export contract, 500 paths, streaming close, client disconnects** in ASGI, WSGI, Flask and Django: traced against the installed instrumentor sources; `finish`/`finalize` always run, held spans cannot leak, partial bodies are never exported.
- **OTLP encoding by concatenating serialized requests in one gzip stream**: valid protobuf merge semantics, covered by tests for all three signals.
- **Shutdown double-drain** (lifespan hook plus atexit): `stop()` unregisters the atexit hook. The LIFO ordering leaves at most a handful of straggler spans in an orphan spool file, cleaned up by a later process after two hours.
- **Two-hour outage behaviour**: one probe POST per cycle, head-of-queue expiry after 59 minutes from first attempt, disk capped at 50 MB with metrics evicted last, recovery at 10 files per cycle with original timestamps. Matches MIGRATION.md.
- **`django.contrib.admindocs.views` import at settings time** pulls in `django.contrib.admin` and `django.forms` (about 45 ms on top of 220 ms for Django itself). Works before app loading and most projects have admin installed anyway.
- **Sentry integration** without `sentry_sdk.init()`, `otel.instrument_*` on missing packages, multiple `init()` calls, invalid or missing tokens, `manage.py` commands: all behave as intended.
- **`ASGIActivationShim` resetting OTel context** at the outermost layer: correct for the supported layouts; BlackSheep's user-instrumented case passes `reset_context=False`.
- **Redaction ordering**: all masking runs in `ApitallySpanExporter` on the export thread before `Spool.append`; user-attached processors receive the original snapshot without the stash.
