# Apitally Python SDK v1 — Design

High-level design decisions for the v1 refactor of the Apitally Python SDK. Captures the structural and strategic choices made during the early design discussion. The wire-format contract lives in `spec.md` (copied from the cloud repo); this document is the SDK-side counterpart.

The headline goal: v1 is a clean break, OpenTelemetry-based, with one-line setup as the #1 priority. The SDK leans on stock community OTel instrumentations wherever possible and adds Apitally-specific functionality (body capture, consumer attributes, startup event, redaction) as thin layers on top.

## 1. Branching and release

- **Branch**: `v1` off `origin/main`, modify in place. No detached/clean-slate branch.
- **0.x**: feature-frozen the moment v1 work starts. Security/maintenance patches only — no formal sunset date. The Hub backend keeps accepting 0.x traffic indefinitely per the spec; this is the SDK-side commitment.
- **Package**: same PyPI name `apitally`. Major version bump to `1.0.0`. Users pinned `<1.0` are unaffected; unpinned upgrades will break and hit a clear migration error message on import.
- **Pre-release**: public alphas (`1.0.0a1`, `1.0.0a2`, ...) on PyPI for early adopters. No formal beta or RC. Ship `1.0.0` GA when alpha feedback settles.
- **Cloud dependency**: `otlp.apitally.io` is already live in production. No external blocker.

## 2. Integration with existing OTel setups

The SDK detects whether the user already has OpenTelemetry configured and behaves accordingly. Only the `TracerProvider` participates in cooperative mode; the `MeterProvider` and `LoggerProvider` are always ours.

Detection and provider registration happen at activation (§7), not inside `init_apitally(...)`. User OTel setup runs at import or startup time — including inside lifespan startup handlers, which §7's trigger deliberately waits out — and is complete before the first request, so activation-time detection makes the mode choice independent of where `init_apitally(...)` sits in the startup sequence. Deferring loses nothing: instrumentors installed at configure time obtain `ProxyTracer`s from the not-yet-set global, and a `ProxyTracer` resolves to the real provider on first use after registration. Detection goes through `trace.get_tracer_provider()` (not the private global) so a provider configured lazily via `OTEL_PYTHON_TRACER_PROVIDER` is correctly seen as cooperative; "no existing OTel" means the call returns a `ProxyTracerProvider`. Residual: OTel setup that only runs after startup completes (e.g. inside a request handler) still loses — the global is set-once, so the user's later `set_tracer_provider` is warn-and-ignored by OTel while Apitally keeps working. Documented ordering note, not otherwise handled.

- **No existing OTel**: install our own `TracerProvider` with our OTLP `BatchSpanProcessor`, resource, span limits, and an explicit `ALWAYS_ON` sampler. Sampling is never Apitally's mechanism — the §3 processor is the only drop point, and Apitally coverage must not depend on an upstream's tracing-cost sampling: without an explicit sampler, OTel defaults to `ParentBased(ALWAYS_ON)`, which turns a SERVER span under an upstream-unsampled `traceparent` into a `NonRecordingSpan` the processor never sees (violating spec § 6.5). Passing the sampler explicitly also means the SDK never falls back to reading `OTEL_TRACES_SAMPLER` (§14). Owned trade-off: such requests are recorded and propagate sampled=1, so services downstream of this one see an enabled trace even though the original upstream sampled it out.
- **OTel TracerProvider already configured** (e.g., user has Datadog, Honeycomb, or their own collector): don't replace it. Attach our OTLP `BatchSpanProcessor` additively to the existing `TracerProvider`. The user's sampler applies: spans it drops never exist for our processor, so request-log coverage follows their sampling rate, while the §4 histograms (recorded in framework glue, sampling-independent) stay complete. At activation, inspect the provider's sampler and warn (once) only when it is recognizably lossy — `ALWAYS_OFF`, `TraceIdRatioBased`, or a `ParentBased` whose root is not `ALWAYS_ON` — naming the coverage consequence and the remedies (raise the rate, or own-it-all mode). Everything else, including unrecognized custom/vendor samplers, gets a DEBUG line with the sampler description and no warning: §9 reserves WARNING for known, actionable loss, and an unclassifiable sampler is neither. Residual under the OTel default `ParentBased(ALWAYS_ON)`: requests arriving with an unsampled upstream `traceparent` are not recorded — the own-it-all bullet above closes exactly this hole; in cooperative mode it is a documented limitation, not a warning. No sampling bypass.
- **MeterProvider and LoggerProvider**: always construct our own, regardless of what the user has. Both are private instances — never registered via `set_meter_provider` / `set_logger_provider`. The OTel globals are set-once (a second registration warns and is ignored), so registering ours would clobber or lose a race against the existing metrics/logs pipelines of exactly the users cooperative mode serves. Instead the providers are passed explicitly — but only where Apitally consumes the output: `meter_provider=` to `SystemMetricsInstrumentor` (§12), our provider's `get_meter(...)` for histogram construction (§4), and `logger_provider=` to the root-logger `LoggingHandler` (§10). Framework instrumentors never get our `meter_provider`: they record their own semconv HTTP metrics onto whatever provider they're handed, which would ship dead payload in own-it-all mode and hijack the user's `http.server.*` metrics in cooperative mode (instrumentors are process-global singletons — first instrument wins). Without it they fall back to the global provider: a no-op in own-it-all mode, the user's own pipeline in cooperative mode — each party gets exactly the metrics they expect.

### Attribute length limit

In own-it-all mode, construct the `TracerProvider` with `SpanLimits(max_attribute_length=65_536, max_span_attribute_length=65_536)`. Both fields are pinned explicitly: the OTel default is unlimited, but `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT` and `OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT` would otherwise apply — and the span-specific env var takes precedence over a constructor default for `max_span_attribute_length`, so passing only the general field leaves a side door open. 65 KiB comfortably fits a 50,000-byte body (per spec § 6.3).

In cooperative mode the SERVER span comes from the user's `TracerProvider`, so the user's `SpanLimits` apply at `set_attribute` time — a limit below 65,536 (commonly via `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT` set for another backend) silently clips a captured body mid-document, violating the never-truncated MUST in spec § 6.3. When body or header capture is enabled in cooperative mode, inspect the user provider's limits at activation — best-effort via the private `_span_limits`; the effective value is `max_span_attribute_length`, which defaults from `max_attribute_length`, and env-var limits (`OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT`, `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT`) are already resolved into `SpanLimits` at provider construction, so the inspection covers them — and if below 65,536, log a warning that captured bodies may be truncated and how to raise the limit. Documented as a cooperative-mode limitation.

### Environment resolution and the `Apitally-Env` header

The `Apitally-Env` header (spec § 4) MUST match `deployment.environment.name` on the Resource. Resolve once at activation (resolution needs the mode — cooperative mode reads the user's Resource) and pass to our OTLP exporter's `headers={...}`.

- **Own-it-all mode**: env = `env=` kwarg / `APITALLY_ENV` / `"prod"`. Set both `deployment.environment.name` on our Resource AND `Apitally-Env` on the exporter from this single value.
- **Cooperative mode**: env = user's `deployment.environment.name` (from `user_provider.resource.attributes`) if present, else `env=` kwarg / `APITALLY_ENV` / `"prod"`. Use this value for `Apitally-Env` on our exporter. We do not modify the user's Resource.
- **Conflict**: if cooperative-mode AND the user's Resource has `deployment.environment.name` AND the caller passed `env=` with a different value, log a warning and use the Resource value. Users who want a separate Apitally env must either change their OTel env or use own-it-all mode.

## 3. Span filtering — single mechanism

Apitally only ingests request-rooted traces. Non-request spans (background workers, schedulers, queue consumers) must not be exported to Apitally.

One mechanism handles this in both own-it-all and cooperative modes: a `SpanProcessor` wrapper around our OTLP exporter.

- On `on_start`: a span whose parent is absent or remote (`span.parent is None or span.parent.is_remote`) is a local root. A local root with `kind == SERVER` → enter the map as `(True, own_span_id)`; any other local root → `(False, None)`. A span with a local parent reads the parent's `span_id` from `span.parent` (a `SpanContext`) and inherits the parent's `(keep, server_span_id)` entry; a lookup miss defaults to `(False, None)`. Apps behind an instrumented gateway or mesh receive `traceparent`, so their SERVER spans have a remote parent — they are still the request boundary (spec § 6).
- OPTIONS and excluded requests are decided at the same point: a SERVER span that would otherwise enter the map as kept instead enters as `(False, None)` when its `http.request.method` is `OPTIONS`, or its `url.path` / `user_agent.original` (old-semconv fallbacks included) matches an exclusion pattern — defaults per spec § 6.8, user path patterns added on top. The stock instrumentors pass these as span-creation attributes, so they are readable via `span.attributes` inside `on_start`; via the shared map this also drops the request's logs (§10). Histogram recording in the glue (§4) is independent: excluded requests are still counted (spec § 6.8), while OPTIONS and unmatched-route requests are not (spec § 7.1). A user-supplied `exclude_on_request` callback (§5) is evaluated at the same point: it receives the SERVER span and a `True` return enters the same `(False, None)` entry — nothing about the request is ever transmitted.
- On `on_end`: if `keep` is true, forward to the wrapped batch processor; otherwise drop. Pop the span_id from the in-flight map. For a kept SERVER span, the user-supplied `exclude_on_response` callback (§5) is evaluated first — `True` drops the span instead of forwarding. The SERVER span is the keystone: without it no request row can materialize server-side, and descendants or logs that already streamed out are discarded as unclaimed orphans at ingest (out-of-order batch arrival forces the ingest to handle those regardless; the GC rule is specified upstream in the spec). Combined with `set_request_attribute(...)`, this lets users exclude on their own business attributes. Callback errors follow §9: warn, treat as not excluded.
- Also on `on_start`: an INTERNAL span whose name ends with ` http send`, ` http receive`, ` websocket send`, or ` websocket receive` and whose instrumentation scope name starts with `opentelemetry.instrumentation.` is entered as `(False, None)` even under a kept SERVER parent — the spec § 6.6 backstop for instrumentors that can't suppress these spans at the source (Starlette, see §4). The websocket variants go beyond § 6.6's http-only wording; the spec generalization to "per-message INTERNAL spans" lands upstream. The scope check is safe here: the SDK sets `instrumentation_scope` on the span before `on_start` fires.
- The `server_span_id` carried in the entry exists so the `LogRecordProcessor` (§10) can stamp `apitally.request.server_span_id` on logs emitted inside arbitrarily nested children of the SERVER.
- In cooperative mode, the user's other exporters still get all spans (background work included). Only our exporter filters.

This is the only filtering mechanism. No global `ApitallyRootSampler`.

## 4. Framework composition

Thin Apitally layer on top of stock community OTel instrumentors.

| Framework | Stock OTel instrumentor | Notes |
|---|---|---|
| FastAPI | `opentelemetry-instrumentation-fastapi` | |
| Starlette | `opentelemetry-instrumentation-starlette` | |
| Django (incl. Ninja, DRF) | `opentelemetry-instrumentation-django` | |
| Flask | `opentelemetry-instrumentation-flask` | |
| Litestar | Litestar's own OTel plugin | First-party plugin, not a community-contrib instrumentor. |
| BlackSheep | `opentelemetry-instrumentation-asgi` (generic fallback) | No community-specific instrumentor exists; generic ASGI is sufficient. Route resolution via router wrap (below), not an instrumentor hook. |

The stock instrumentor produces the SERVER span and sets the standard HTTP attributes — except the body-size pair, which no instrumentor sets as span attributes in any semconv mode (§6). Our framework-specific glue adds:
- Body capture (request + response) — see §6
- `http.request.body.size` / `http.response.body.size` (spec § 6.1) — in the transport middleware, independent of the capture toggles (§6)
- Consumer attribute setter (`set_consumer(...)`) — via the §5 ContextVar
- Histogram recording at request end (the three exponential histograms from the spec) — in the transport middleware, keeping it sampling-independent (§2)
- Startup event (routes + OpenAPI) emission — at activation (§7)
- Sentry event-id linkage (when Sentry is detected) — §15

All of this glue lives in Apitally-owned paths — the transport middleware (§6), the activation shim (§7), the §5 span-handle ContextVar — never in instrumentor hooks. Hooks are a fragile dependency: the contrib instrumentors discard the second caller's kwargs when the framework is already instrumented (next subsection), and §2's sampling-independence claim for histograms rules out the span pipeline as their home anyway.

**BlackSheep route resolution**: wrap `app.router.get_match` (the 0.x mechanism) and, on a match, set `http.route` to the matched pattern and update the span name on the live SERVER span via the §5 ContextVar — the router runs inside the SERVER span's context. The `"*"` pattern (unmatched request, BlackSheep >= 2.4.4) is treated as no route. No `server_request_hook` involved, so this survives pre-instrumented apps like everything else.

**BlackSheep interposition**: `Application.__call__` is a type-level dunder, so the instance the server holds cannot be wrapped at `__call__` (per-instance dunder assignment is ignored by Python's type-based lookup). `__call__` dispatches through `self._handle_http(scope, receive, send)` — an instance-attribute lookup, honored under `MountMixin`, with an ASGI-shaped signature — so `init_apitally` wraps `app._handle_http` with the §7 shim → instrumentor → transport chain. Activation hooks the public `app.on_start` event, which fires on `lifespan.startup`; when the server skips lifespan, `_handle_http` awaits `start()` (firing the same event) before dispatching into the wrapped chain, so the §7 first-request guarantee holds structurally. `_handle_http` is the one private-API dependency, covered by an integration test. Websockets are untracked; `_handle_websocket` stays untouched.

### Semantic conventions

Contrib 0.64b0 emits old HTTP semconv (`http.method`, `http.target`, ...) unless `OTEL_SEMCONV_STABILITY_OPT_IN` includes `http` — a process-global env var latched exactly once, at the first instrumentor initialization anywhere in the process. spec § 6.1 requires stable names, and the §5 callback docs assume them. At the top of configure, when the var is unset, set it to `http/dup`: both name sets are emitted, so Apitally gets stable names while a cooperative user's own backend keeps receiving the old names its dashboards may key on — nothing changes for anyone, no mode branch. A value the user already set is respected. Residual: in a cooperative process where any instrumentor initialized before `init_apitally(...)` ran, the latch already snapped on old-only names — undetectable and unfixable via the env var; spec § 6.1's server-side old-name fallbacks cover ingest, and §6's query redaction handles `http.target` (which embeds the query string in old semconv) — a contract test pins that path.

### Already-instrumented frameworks

Cooperative-mode users may have instrumented the framework themselves (directly or via `opentelemetry-instrument`). The contrib instrumentors guard against double instrumentation — a second `instrument_app` call is discarded (FastAPI/Flask log a generic OTel warning; Starlette no-ops silently). At configure time, detect the guard (per-app `_is_instrumented_by_opentelemetry` attribute; public `is_instrumented_by_opentelemetry` property on the singleton instrumentors; an existing `OpenTelemetryMiddleware` in the chain for the generic-ASGI case; for Litestar, checked in `ApitallyPlugin.on_app_init`, a stock `OpenTelemetryPlugin` in `app_config.plugins` OR an OTel `DefineMiddleware` in `app_config.middleware` — the legacy pre-plugin pattern, which Litestar hoists to app level itself) and skip our instrumentation call rather than triggering the no-op. Because the glue is hook-independent, nothing degrades: SERVER spans from the user's instrumentation reach our processor (cooperative mode attaches to their provider), and the §3 backstop drops the receive/send spans our discarded `exclude_spans` would have suppressed. The adaptation is silent per §9 (DEBUG only) — nothing is lost, so there is nothing to warn about. Documented limitation: a user who instrumented with an explicit non-global `tracer_provider` routes SERVER spans past the provider we attach to, and those requests are invisible to Apitally.

Transport-middleware position on pre-instrumented apps: Starlette — a plain `add_middleware` would insert at `user_middleware[0]`, outside the already-present `OpenTelemetryMiddleware`, so the adapter instead inserts our transport middleware into `app.user_middleware` immediately after the existing `OpenTelemetryMiddleware` entry (the stack builds lazily on first request, and the instrumentor's own `uninstrument_app` mutates the list the same way). FastAPI needs nothing — its instrumentor wraps the entire built stack lazily, so middleware added later still lands inside. Flask needs nothing — its span writes are hook-timed (§6), independent of wrap order. Litestar needs nothing — the `before_send` route fix reads the §5 ContextVar and works identically under the stock plugin (litestar.md).

ASGI integrations are configured with `exclude_spans=["receive", "send"]` where the instrumentor supports it — FastAPI and the generic ASGI fallback for BlackSheep directly, and Litestar via its OTel plugin's `OpenTelemetryConfig`. By default they emit one INTERNAL span per ASGI `receive()` and `send()` call — pure noise per spec § 6.6. `StarletteInstrumentor.instrument_app` (0.64b0) accepts no `exclude_spans`, so Starlette emits these spans and relies on the `ApitallySpanProcessor` backstop (§3) to drop them.

### Metrics pipeline

OTel Python defaults produce explicit-bucket histograms with cumulative temporality — the server drops both (spec § 7.1) — and `ExponentialBucketHistogramAggregation` on its own defaults to `max_scale=20`, outside the spec's MUST range of [-2, +6]. Our `MeterProvider` therefore gets an explicitly configured pipeline: `PeriodicExportingMetricReader(exporter, export_interval_millis=60_000)` wrapping `OTLPMetricExporter(..., preferred_temporality={Histogram: AggregationTemporality.DELTA}, preferred_aggregation={Histogram: ExponentialBucketHistogramAggregation(max_scale=3)})`, where `Histogram` is the SDK instrument class from `opentelemetry.sdk.metrics` (the API class raises at reader construction).

- `max_scale=3` starts at the spec's SHOULD scale of 3 and the SDK only ever downscales from there; with the default 160 buckets, falling below the -2 floor would take a value spread beyond 2^640, unreachable for durations or body sizes.
- The 60 s interval is passed explicitly because the reader otherwise reads `OTEL_METRIC_EXPORT_INTERVAL` from the environment — a user-set value would silently change the heartbeat cadence the liveness contract depends on (spec § 7.3).
- Both overrides are keyed on the histogram instrument class, so the process gauges (§ 12) are untouched: they keep last-value aggregation, which the server accepts per spec § 7.2.

## 5. Public API

### Setup

`init_apitally(app, ...)` per framework. Regular function call with explicit typed kwargs; IDE autocomplete and type hints work properly. Replaces today's `app.add_middleware(ApitallyMiddleware, ...)` pattern.

For Django: `init_apitally(...)` with no `app` argument, called at the **end of `settings.py`**. This is the canonical Django integration location (mirroring Sentry and Logfire), placed after `MIDDLEWARE` is defined so `DjangoInstrumentor` can prepend its middleware safely.

For Litestar: no `init_apitally(app, ...)` — `Litestar.plugins` is built from a frozenset at construction and there is no late-registration API (see `litestar.md`). Setup is `Litestar(plugins=[ApitallyPlugin(...)])` at construction; the plugin takes the same kwargs and runs the same configure/activate path from its `on_app_init` hook. Document the asymmetry in the user-facing setup docs.

Behavior:
- **Idempotent**: re-calling with the same args is a no-op. Different args: last call wins (reconfigure exporters, env, redaction, body-capture toggles).
- **Returns `None`**: side-effecting function. No return value users can grab.
- **Explicit kwargs**: each per-framework function signature spells out its kwargs. Some framework-specific kwargs exist (e.g., FastAPI's `app_version`, `openapi_url`).

### Cross-framework kwargs

| Kwarg | Type | Default | Notes |
|---|---|---|---|
| `write_token` | `str` | — | Required (or via env var) |
| `env` | `str` | `"prod"` | Apitally environment name |
| `disabled` | `bool` | `False` | Plain boolean. Skip activation entirely. |
| `capture_logs` | `bool` | `True` | Auto-install root logger bridge — see §10 |
| `exclude_on_request` | `Callable[[ReadableSpan], bool]` | `None` | `True` = exclude, decided at span start (§3). Guarantee: nothing about the request is transmitted. Filter on semconv attributes (`url.path`, `http.request.method`, `user_agent.original`, ...). |
| `exclude_on_response` | `Callable[[ReadableSpan], bool]` | `None` | `True` = exclude, decided at SERVER span end (§3). Guarantee: the request is not recorded; mid-request telemetry may have transited and is discarded at ingest. Filter on `http.response.status_code`, `apitally.consumer.*`, custom attributes. |
| `mask_request_body` | `Callable[[ReadableSpan, bytes], bytes \| None]` | `None` | Replaces the captured request body before pattern redaction; `None` or a raise → the literal `<masked>` (fail closed, §6). Renamed from 0.x `mask_request_body_callback`. |
| `mask_response_body` | `Callable[[ReadableSpan, bytes], bytes \| None]` | `None` | Same for response bodies. Renamed from 0.x `mask_response_body_callback`. |
| Body/header capture toggles, redaction extras, excluded paths, etc. | (per legacy SDK option names) | (per spec) | Off by default — see §6 |

### Top-level public surface

| Symbol | Purpose |
|---|---|
| `init_apitally(...)` | Per-framework setup. Litestar uses `ApitallyPlugin` instead — see Setup above. |
| `set_consumer(identifier, name=None, group=None)` | Sets `apitally.consumer.*` on the active SERVER span. |
| `set_request_attribute(key, value)` | New in v1. Sets arbitrary attribute on active SERVER span. |
| `capture_exception(exc)` | New in v1. Records an exception event on active SERVER span. |
| `instrument(func)` | Decorator. Wraps a function in a span. Kept from existing `otel.py`. |
| `span(name, attributes=None)` | Context manager. Kept from existing `otel.py`. |
| `instrument_httpx`, `instrument_sqlalchemy`, etc. | Lazy-import thin wrappers around community instrumentors. Kept as-is. |

### Locating the active SERVER span

Every "active SERVER span" write site — `set_consumer`, `set_request_attribute`, `capture_exception`, the §6 body-capture middleware, the §15 Sentry hook — resolves the span through one mechanism: a ContextVar set by `ApitallySpanProcessor.on_start` for every local-root SERVER span (the same classification §3 computes), independent of the OPTIONS/exclusion/keep decision. Keep-vs-drop is enforced solely by the §3 map at `on_end`, so writes to a span that will be dropped stay local and are never exported — and the var always holds the current request's span, never a stale handle from a previous request in a reused worker-thread context. `get_current_span()` is not usable for this: inside a child span (DB call, `instrument()` decorator) it returns the child, and OTel has no public API to walk up to the SERVER span. The ContextVar is set synchronously at span start in the request's execution context and propagates into async handlers (task context) and sync-in-threadpool handlers (anyio's `to_thread.run_sync` copies context), so it is visible wherever user code runs, with no per-framework wiring and no dependency on instrumentor hooks.

Two documented caveats: in cooperative mode a user sampler that drops the request means the SERVER span never starts recording, the ContextVar stays empty, and these APIs no-op — the same coverage limitation §2 already owns. And the var can leak into post-response background tasks; writes to the ended span are dropped by the SDK with a warning, which is the intended outcome.

### Removed

- All of `apitally.client.*` (Hub transport, threading/asyncio variants, message queues, validation/server error capture, sentry bridge).
- `client_id` everywhere — replaced by `write_token`.
- `ApitallyMiddleware` class — replaced by `init_apitally(...)`.
- Separate validation/server error capture API — cloud derives these server-side from standard OTel exception events on traces.
- `exclude_callback(request, response)` — replaced by `exclude_on_request` / `exclude_on_response`, which operate on the SERVER span's OTel attributes instead of bespoke request/response dicts.
- `consumer_callback` (and its deprecated alias `identify_consumer_callback`) — replaced by calling `set_consumer(...)` from auth middleware or dependencies; the §5 ContextVar makes it framework-independent, where the 0.x callback dragged per-framework request types into the API. This is the migration path.
- `proxy` — the OTLP HTTP exporter's transport (a requests `Session` with `trust_env=True`) honors the standard `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` env vars.
- `capture_client_disconnects` — was Starlette/FastAPI-only and off by default in 0.x; the 499-rewrite has no OTel equivalent.

## 6. Body and header capture

### Default: OFF

Body capture and header capture are **disabled by default**. For bodies and headers the privacy posture matches the legacy SDK — users explicitly opt in to capture payload content. Defaults give a strong product without payload data: traces, metrics, logs, exceptions, consumer attribution. Log capture defaults deliberately differ from 0.x — see §10.

### Implementation: transport-level

One ASGI middleware covers all ASGI frameworks (FastAPI, Starlette, Litestar, BlackSheep, Django-ASGI). One WSGI middleware covers all WSGI frameworks (Flask, Django-WSGI). Per-framework adapters wire up the right transport middleware.

Reading the body is a transport concern, not a framework concern. Per-framework duplication is what made `client/request_logging.py` 573 lines; transport-level is the path to "simple, lightweight, minimal SDK code."

Ordering rule: the transport middleware MUST run inside the instrumentor's middleware. On ASGI and Django the SERVER span ends when the instrumentor's layer completes the response, so attributes set from an outer middleware would land after span end and be dropped by the SDK (with a per-request SDK warning — log spam on top of data loss). Per framework: FastAPI — automatic, the instrumentor patches `build_middleware_stack` and wraps the entire built stack; Starlette — the instrumentor attaches via `add_middleware`, which inserts at `user_middleware[0]` (last-added is outermost), so the adapter attaches our transport middleware BEFORE calling `instrument_app`; Flask — attach ours before instrumenting so our middleware replaces `wsgi.input` before Flask reads it (span-lifetime ordering does not apply on Flask — next paragraph); Django — our middleware entry goes after the OTel middleware, which the instrumentor inserts at `MIDDLEWARE[0]`. The full stack from outside in: §7 activation shim → instrumentor middleware (SERVER span) → transport middleware (body capture).

**Flask span writes are hook-timed, not middleware-timed.** The Flask instrumentor starts the SERVER span in `before_request` and ends it in `teardown_request`, which Flask runs inside its own `wsgi_app` `finally` — before the response iterable reaches any WSGI middleware, inner or outer. No WSGI layer can set response attributes while the span is alive. The Flask adapter therefore writes at two Flask-side points instead: request body and response headers at our wrapped `start_response` (fires inside `wsgi_app` while the span records, with wire-final headers), and the response body in an `after_request` hook (runs before teardown), reading `response.get_data()` when `direct_passthrough` is false. Streaming/direct-passthrough Flask responses are not captured — one documented limitation, no machinery. The WSGI middleware's role on Flask is transport only: `wsgi.input` buffering per the Content-Length gate, the wrapped `start_response`, and duration/size accounting on the response iterable.

Capture mechanics (per spec section 6.3), in pipeline order — both capture decisions are header-only, so a request that won't be captured costs zero body I/O:
- MIME allowlist first (spec § 6.3), from the request headers / response-start headers, before any body is read or buffered: `application/json`, `application/problem+json`, `application/vnd.api+json`, `application/ld+json`, `application/x-ndjson`, `text/markdown`, `text/plain` (case-insensitive prefix match, ignoring `; charset=...`). Outside the list → no attribute set, regardless of size — the body is never touched (on WSGI, `wsgi.input` isn't even replaced).
- Size cap 50,000 bytes (spec § 6.3): an over-cap body sets the attribute to `<body too large>` — never a truncated body.
- Request bodies, WSGI: capture only when `CONTENT_LENGTH` parses to an int. Over cap → sentinel without reading a byte; otherwise `read(content_length)` and re-emit as `BytesIO` (the 0.x mechanism — see `wsgi.md`). Never read past or without Content-Length: PEP 3333 makes EOF simulation a SHOULD, and raw-socket servers (wsgiref, werkzeug dev server) block on over-reads until the client gives up. Chunked/absent-length request bodies are not captured, matching 0.x.
- Request bodies, ASGI: accumulate `http.request` messages with a running length check, always forwarding every chunk; crossing the cap discards the buffer and sets the sentinel.
- Response bodies (both transports): accumulate sent chunks under the same running-length rule.
- Mask callbacks (spec § 6.3): `mask_request_body` / `mask_response_body` (§5) are called with the SERVER span and the captured body bytes. Return value replaces the body; `None` sets the attribute to the literal `<masked>`; a raising callback also yields `<masked>` — fail closed, never export a body the user tried to mask.
- Then: parse JSON if applicable → redact → re-serialize → set `apitally.request.body` / `apitally.response.body` on the active SERVER span (§5 ContextVar). Pattern redaction applies to whatever the mask callback returned.
- Default redaction patterns per spec § 6.7. User-supplied patterns add to defaults, never replace.

### Header capture

`http.request.header.<name>` / `http.response.header.<name>` (spec § 6.1) are set by the transport middleware on the SERVER span (§5 ContextVar), at the write sites already established for body and size: ASGI — request headers from the scope at request entry, response headers from the `http.response.start` message in the send wrapper; Flask — both at the wrapped `start_response` (request headers from the closure-captured environ, response headers wire-final); Django — in the Django glue. When the per-direction toggle is on (legacy option names, off by default per this section), all headers are captured and § 6.7 redaction runs before each attribute is set — shared `redaction.py`, same pattern table as query params and body fields — matching 0.x capture-all-then-redact semantics: a redacted header keeps its name, its value becomes `[REDACTED]`. Redact-before-set matters beyond spec compliance: in cooperative mode the attributes land on the user's span, so an unredacted write would leak raw values to the user's other exporters.

Attribute keys use the stable-semconv normalization — lowercase header name, dashes preserved (`http.request.header.content-type`) — and values are list-valued per the semconv; repeated ASGI headers become multiple list elements, WSGI's environ collapses repeats into one comma-joined element. OTel Python contrib still emits the pre-stabilization underscore form (`content_type`) — the semconv opt-in machinery never covered header keys — so the server folds `_` to `-` when parsing the suffix (upstream spec task).

Users can independently enable the instrumentors' own capture (`OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*`), which puts header attributes on exported spans governed only by the user's sanitize list. The § 6.7 guarantee is closed at the last SDK-owned point: the `ApitallySpanProcessor`'s `on_end` rewrite (below) also applies the header patterns to any `http.request.header.*` / `http.response.header.*` attributes on the forwarded copy, in either normalization. For headers our transport set, this pass is a no-op — they were redacted before set.

### Body size attributes

`http.request.body.size` and `http.response.body.size` (spec § 6.1) are set by the transport middleware on the SERVER span (§5 ContextVar), independent of the capture toggles — no stock instrumentor sets them as span attributes in any semconv mode (the ASGI instrumentor records sizes only as metrics on a meter §2 withholds; WSGI/Django have no size handling at all). Values keep 0.x semantics:

- **Request size**: Content-Length header, both transports. Header absent but body capture accumulated the full body anyway (ASGI) → backfill from the captured length. Chunked WSGI request bodies have no determinable size → attribute unset.
- **Response size, ASGI**: Content-Length from the `http.response.start` headers when present and not chunked; otherwise a running `+= len(chunk)` counter in the send wrapper, written before forwarding the final message (span still alive per the ordering rule). Counting only — no buffering beyond what capture already does.
- **Response size, Flask**: wire-final Content-Length at the wrapped `start_response`; absent (streaming) → unset. **Django**: Content-Length header or `len(response.content)` for non-streaming, in the Django glue.

The same values feed the two spec § 7.1 size histograms, so request logs and metrics agree by construction; an unknown size skips both the attribute and the histogram observation (spec § 7.1's "when the size is known"; § 6.1's "regardless of body capture" is being softened upstream to "when the size is determinable").

### Query param and header redaction: span processor

The stock instrumentors set `url.query` (legacy: `http.target`, `http.url`) from the raw query string. OTel's built-in redaction doesn't cover this: `redact_url` applies only to legacy `http.url` and only to four exact param names (`AWSAccessKeyId`, `Signature`, `sig`, `X-Goog-Signature`) — it never touches `url.query` and doesn't satisfy the spec § 6.7 patterns. The SDK owns query redaction, and the owner is the `ApitallySpanProcessor` (§3), not the transport middleware or per-instrumentor hooks.

Mechanics: in `on_end`, before forwarding a kept span to the wrapped batch processor, apply the spec § 6.7 query patterns (defaults + user-supplied, matched against param names) to `url.query`, `http.target`, and `http.url`, apply the § 6.7 header patterns to any `http.request.header.*` / `http.response.header.*` attributes (either key normalization — see "Header capture"), and forward a rewritten copy of the `ReadableSpan`. Span attributes are frozen once the span ends, so the copy is the mechanism — the original span is never mutated, and in cooperative mode the user's other exporters see the unmodified span, consistent with §3. Redacting in `on_start` is not viable: the ASGI middleware re-applies its raw attribute dict to the span right after span start, which would clobber the rewrite.

## 7. Activation model

Setup happens at app start; activation (network threads, heartbeat, startup event) is gated to ensure correctness in fork-based servers and non-server contexts.

### Configure phase (eager, all frameworks)

`init_apitally(...)` does:
- Record and validate configuration. No provider registration — mode detection and registration happen at activation (§2).
- Set `OTEL_SEMCONV_STABILITY_OPT_IN=http/dup` when unset — before any instrumentor initializes (§4).
- Attach our ASGI/WSGI transport middleware to the app.
- Install stock instrumentor — after our middleware, per the §6 ordering rule (required on Starlette and Flask, irrelevant on FastAPI, settings-based on Django).
- Install the activation shim (outermost — see below).
- Connect Django's `request_started` signal (Django only).
- Build meter, histograms, observable gauges (on our private `MeterProvider` — no global involved, no ordering concern).
- No threads, no network, no fork-unsafe objects.

### Activate phase

Nothing activates at import time — `init_apitally(...)` only configures. Activation is gated on evidence the process is actually serving:

- **ASGI frameworks**: activate when the shim observes the app's `lifespan.startup.complete` message on its send path OR on the first request, whichever comes first. Triggering on startup *completion* — not on receipt of `lifespan.startup` — means the app's startup handlers run first, so OTel setup inside a lifespan context manager (FastAPI's documented init pattern) is honored by §2's mode detection; the server doesn't serve until the message is sent, so activation still precedes all requests. A failed startup sends `startup.failed` instead — no activation, first-request trigger catches it if the server serves anyway. ASGI servers run the lifespan protocol at boot, so a normally-deployed app activates at server start — startup event and heartbeat fire before any traffic. Running with lifespan disabled is not a supported mode; the shim's first-request trigger still catches it best-effort on shim-wrapped frameworks, while Litestar — which has no pre-span seam for a first-request trigger — activates solely via an `on_startup` hook registered by the plugin (litestar.md). Imports trigger neither.
- **WSGI frameworks (Flask)**: no lifespan exists — activate on the first request through our outermost activation shim (below).
- **Django**: activate on first `django.core.signals.request_started`. settings.py can't distinguish "about to serve HTTP" from "about to run `migrate`" or "Celery worker bootstrapping," so the signal is the earliest safe gate. New Relic uses the same pattern.

This kills the phantom-instance class at the root: pytest collection, Celery workers importing task modules, alembic's `env.py`, and REPLs all import the app (configure runs) but never emit a lifespan event or serve a request, so they never activate. Test-environment detection (below) remains as a cheap extra guard, no longer the primary defense.

Activation runs in order: detect the mode and register/attach the `TracerProvider` (§2), run the deferred inspections (sampler, span limits, env resolution — §2), construct exporters, start the exporter and heartbeat threads, send the startup event.

The activation trigger always fires before the triggering request's SERVER span starts, so the first request is recorded normally rather than swallowed by a pre-registration `ProxyTracer` no-op span. On ASGI, lifespan startup precedes all requests; when lifespan is disabled, the first-request trigger requires the shim to sit outside the instrumentor's SERVER-span layer (the §6 transport middleware sits inside and would be too late — the span would already exist, created against the unresolved proxy). Shim attachment per framework: FastAPI — the instrumentor wraps the entire built middleware stack, so `add_middleware` cannot reach the outermost position; instead, after our own instrument call, chain-patch `app.build_middleware_stack` and wrap the returned stack in the shim (the stack builds lazily on first call, so an init-time patch lands in time; ours must be applied after the instrumentor's — last patcher is outermost). Starlette — `add_middleware(shim)` AFTER `instrument_app` (last-added is outermost). BlackSheep — the shim is the outermost layer of the `_handle_http` wrap (§4). Flask/WSGI — wrap `wsgi_app` after the instrumentor so the shim encloses everything. Django — `request_started` fires in the handler before the middleware chain runs, ahead of the OTel middleware's span start; no shim needed. The shim is a few lines: check an activation flag, activate once, delegate.

Activation starts the exporter and heartbeat threads. Under `gunicorn --preload` (and uWSGI without `lazy-apps`) the master imports the app but never serves: configure runs there, activation doesn't. Each worker activates itself post-fork on its own first lifespan event or request, minting its own `service.instance.id`. The fork handlers (next section) are therefore a backstop for processes that fork after having activated, not the primary preload mechanism.

### Fork safety

Forking a multi-threaded process is deprecated from Python 3.12 on and risks deadlock: the child inherits any lock held by a thread that does not survive the fork. Activation runs threads, so they must not be left alive across a fork.

With serving-gated activation, no process manager forks an activated process: gunicorn and uWSGI masters only configure, uvicorn's multi-worker supervisor uses the spawn context, and autoreloaders respawn via subprocess. The fork handlers cover the one real remaining scenario: application code forking after serving began — a `multiprocessing` pool with the fork start method (Linux default) created from a request handler or from startup code that runs after activation. We register `os.register_at_fork` handlers once, in the configure phase (registration itself is thread-free):

- **before**: if activated, quiesce so the process is single-threaded at the instant of `fork()`. Traces/logs: shut down the batch processors (signal, then join — their bounded final flush is unavoidable, there is no detach API). Metrics: detach the reader via `MeterProvider.remove_metric_reader` — detaching nulls the reader's collect callback before shutdown, so the final tick exports nothing and the fork path stays network-free; our reader subclass no-ops `collect()` while quiescing so the SDK's spurious not-registered warning never fires. The discarded sub-minute of accumulated metrics is acceptable: re-activation's next interval lands well within the 180 s liveness window (spec § 7.3). This removes both the deprecation warning and the inherited-lock deadlock. A process that never activated owns no threads — no-op.
- **after, in child**: reset to configured. Discard inherited pipeline state, clear the activation flag, no auto-activation. With activation gated on serving, the child of an activated process is a `multiprocessing` pool worker or forked job runner, not a server — auto-activating it would recreate the phantom-instance class the activate phase eliminates. If the child ever does serve (crosses a lifespan/request gate), it activates normally and mints its own `service.instance.id`.
- **after, in parent**: re-activate immediately via fresh-instance construction (below). An activated forking parent is by construction a serving process (e.g. a request handler using `multiprocessing` with the fork start method, the Linux default). Same process, same `service.instance.id` — the heartbeat resumes with no gap beyond the fork window.

Re-activation constructs fresh instances — OTel shutdown is terminal: `BatchProcessor.shutdown()` sets a permanent `_shutdown` flag (emit rejects telemetry forever), the OTLP exporters' shutdown is likewise terminal, and OTel's own `_at_fork_reinit` restarts the worker thread without clearing the flag. Traces: the provider-registered `ApitallySpanProcessor` (§3) stays registered — `TracerProvider` has no API to remove a processor — and swaps its wrapped `BatchSpanProcessor` + exporter for newly constructed ones. Logs: same swap inside our provider-registered log processor (§10), which wraps the batch export path. Metrics: no wrapper — the `MeterProvider` is ours (§2), the before-fork handler already detached the old reader, so re-activation just attaches a fresh `PeriodicExportingMetricReader` + exporter via `add_metric_reader`. Interaction with OTel's own fork handlers: `os.register_at_fork` runs after-in-child callbacks in registration order, so ours (registered at configure) fires before OTel's per-instance ones (registered when each batch processor and periodic reader is constructed); in the child those fire against the old shut-down instances and start worker threads that exit immediately because the shutdown flags survive the fork. Harmless, but our handler must not assume it runs alone. Keep strong references to the swapped-out processors and readers: OTel's fork handlers hold weak references to them, and a garbage-collected instance produces unraisable `TypeError` noise on a later fork.

These handlers only cover forks that go through Python's `os.fork()` — gunicorn's path, and `multiprocessing`'s fork start method. uWSGI forks workers in C and by default calls none of CPython's fork hooks — which is fine under the serving-gated activate phase: a uWSGI master only configures, so it owns no threads at fork time, and each worker activates independently on its first request. C-level forks of an already-activated serving process are rare enough that v1 does not handle them (no PID tracking).

A single-process deployment never forks: no handler fires and the startup threads keep running, which is correct by default.

Non-serving contexts stay inert regardless: Django management commands, Celery workers, pytest, REPLs, and autoreloader parents never emit a lifespan event, serve a request, or fire `request_started`, so they configure but never activate; test-environment detection catches the rest.

### Test-environment auto-detection

Checked at activation boundary (not on every request):

- `os.environ.get("PYTEST_CURRENT_TEST")` set → skip activation.
- `sys.argv[1:2] == ["test"]` (Django `manage.py test`) → skip activation.
- `os.environ.get("APITALLY_DISABLED")` truthy → skip activation.
- `disabled=True` kwarg → skip activation.

## 8. Idempotency and re-call semantics

- `init_apitally(...)` is idempotent. Re-calling with the same args: no-op.
- Different args: last call wins. Reconfigure what can be reconfigured (write_token, env, redaction config, body-capture toggles, exporter endpoint). Provider-level pieces that can't be swapped after construction (e.g., the global sampler) stay as the first-call set.
- Implementation: module-level singleton holding last-applied config; diff on re-call.

## 9. Error handling — never break the app, ever

**SDK invariant**: errors never propagate to user code. The app keeps running regardless of what's wrong with Apitally.

Severity-mapped logging:

| Situation | Behavior |
|---|---|
| Setup error (invalid token, missing framework dep, OTel misconfig) | `logger.error(...)`. `init_apitally(...)` returns normally; the user's app still starts. |
| Runtime hot-path error (middleware, processor, hook callback) | `logger.warning(...)` or `logger.exception(...)`. The request still completes; that one piece of telemetry is dropped. |
| Background export failure | OTel SDK's own logging — leave it. |

Apitally never raises. `try/except Exception` (not `BaseException`) wraps every entry point.

**Logging posture: quiet by default.** The SDK must be unintrusive — users hate log spam, and most SDK-internal detail means nothing to them. WARNING level is reserved for conditions where Apitally data is being lost, degraded, or misattributed AND the user can act on it (the §2 cooperative sampler/span-limit cases, the env conflict, rejected credentials); each such warning fires once per process, names the consequence, and names the remedy. Everything else — mode selection, adaptation to existing instrumentation, lifecycle transitions — is DEBUG on `apitally.*` loggers. When the SDK adapts automatically with nothing lost, it adapts silently.

**Credential invariant**: error paths never interpolate the raw write token into log messages — it is a bearer credential (spec §3). When a message references the token (e.g. invalid format), log a masked form only: the `apt_` prefix plus the first few characters, e.g. `apt_3kPm…`.

Debuggability requirement: users must know if something is broken. Visible errors in logs. The SDK doesn't quietly do nothing.

## 10. Logs

Auto-install OTel's `LoggingHandler` (from `opentelemetry-instrumentation-logging` — the copy in `opentelemetry-sdk` is deprecated in its favor) on the root logger as part of `init_apitally(...)`. Captures stdlib `logging` output and routes it through the LoggerProvider. Installed directly via `addHandler(LoggingHandler(logger_provider=...))` with our private provider — not via `LoggingInstrumentor().instrument()`, which wires the global LoggerProvider (§2).

- Level: `NOTSET` — capture everything that makes it through user-configured per-logger thresholds.
- Opt out via `capture_logs=False` kwarg for users with strong opinions about their logging stack.
- **Default-on is a deliberate departure from 0.x**, where log capture was double opt-in (request logging enabled plus `capture_logs=True` on top). Rationale: one-line setup is the #1 priority, and request-scoped logs are core to the strong-without-payload default experience (§13). The trade-off: log messages can embed the same sensitive values the body/header opt-ins protect, so upgrading users start exporting log content unless they opt out — the migration guide must call this out explicitly.
- **No log-content redaction in v1**: the §6 redaction patterns apply to query params, headers, and body fields only; log record messages are exported verbatim (spec § 8). Users who log sensitive data must sanitize at the source or set `capture_logs=False`.
- The handler attaches the active OTel context to each emitted record; the record's `trace_id` and `span_id` derive from it at emit time.
- We additionally need `apitally.request.server_span_id` on every request-scoped record — the cloud ingester reads it directly (spec § 9) to derive `request_uuid = hash(trace_id + server_span_id)` and join logs to requests. The handler's `span_id` is the immediate active span (often an INTERNAL child of SERVER) and cannot be used.
- Mechanism: extend the in-flight map maintained by the `ApitallySpanProcessor` (see §3) from `span_id → keep_bool` to `span_id → (keep_bool, server_span_id)`. On `on_start`: SERVER span with absent or remote parent → `(True, own_span_id)`; other local root → `(False, None)`; child with local parent → inherit parent's entry (lookup miss → `(False, None)`). On `on_end`: pop.
- Our `LogRecordProcessor.on_emit(...)` reads `record.span_id`, looks it up in the same in-flight map, and writes `apitally.request.server_span_id = server_span_id.hex()` onto the record.
- The same processor drops any record without an `apitally.request.server_span_id` resolution, UNLESS its instrumentation scope is `apitally` (preserves the startup event, which is emitted on the logs signal under scope `apitally` per spec § 9 with no request context).
- Records from `apitally.*` and `opentelemetry.*` loggers are never bridged: the SDK's and OTel's own logs stay out of the export — no self-noise in customer request logs, no export-failure feedback loop. They still reach the user's own sinks via the root logger, so §9's loudness contract is untouched; the startup event is unaffected because it is emitted directly on the LoggerProvider, not through the bridge.
- Net effect: only request-scoped logs reach the OTLP export, and every one carries the correct SERVER span id regardless of how deeply nested the emitting span was. No ContextVar. No dependency on middleware ordering.

## 11. Dependencies

### Required (always installed)

- `opentelemetry-api >= 1.43.0`
- `opentelemetry-sdk >= 1.43.0`
- `opentelemetry-exporter-otlp-proto-http >= 1.43.0` — HTTP/protobuf transport only; gRPC NOT bundled (see below)
- `opentelemetry-instrumentation >= 0.64b0` (base class for instrumentors)
- `opentelemetry-instrumentation-logging >= 0.64b0` (logging bridge — see §10)
- `opentelemetry-instrumentation-system-metrics >= 0.64b0` (process gauges; pulls `psutil` transitively)

The floors are the version set every §3/§4/§7/§10 behavior was verified against; `MeterProvider.add_metric_reader`/`remove_metric_reader` (§7) requires sdk 1.43.0, and the logging bridge handler requires instrumentation-logging 0.61b0+. Contrib instrumentors pin their matching `opentelemetry-instrumentation`, so the 0.64b0 floor propagates to the per-framework extras.

### Dropped from required

- `backoff` — OTel SDK handles retry/backoff.
- `psutil` as direct dep — transitive through `opentelemetry-instrumentation-system-metrics`.

### Why HTTP/protobuf only, not gRPC

- Apitally's typical span throughput is far below the threshold where gRPC's per-export efficiency wins. The OTel maintainers' Go benchmark shows the gap is insignificant for batches ≥ 100 spans; HTTP is actually more efficient for small batches.
- HTTP/protobuf goes through every CDN, reverse proxy, WAF, and corporate egress proxy. gRPC requires HTTP/2 end-to-end; many corporate networks silently break it.
- gRPC ships ~10 MB of compiled wheels (grpcio); HTTP exporter is small.

### Per-framework extras

Each `apitally[<framework>]` pulls in the framework itself + the stock community instrumentor for that framework.

### Python version

3.10+. Same as current. Nothing in v1 forces a bump.

### Framework version floors

Match whatever the corresponding OTel community instrumentor minimally supports. Wherever the stock instrumentor accepts an old version cleanly without hacks, we do too.

No forced changes — all floors stay where they are. `opentelemetry-instrumentation-starlette` declares `starlette >= 0.13` unbounded since 0.58b0 (earlier releases pinned `< 0.15`), so the existing 0.26.1 floor stands. Verify each floor against the pinned instrumentor version's `_instruments` metadata when finalizing.

## 12. Process gauges

Per spec section 7.2: three gauges required (`process.cpu.utilization`, `process.memory.usage`, `process.uptime`).

- `process.cpu.utilization` + `process.memory.usage`: via `opentelemetry-instrumentation-system-metrics`, configured to emit ONLY those two metrics (the library defaults to ~30 metrics including disk/network/GC): `config={"process.cpu.utilization": None, "process.memory.usage": None}`. `None` values, because these two instruments emit a single unlabeled observation each — mode lists like `["user", "system"]` apply to other instruments only. Both callbacks run in one reader collection cycle, so their data points share one timestamp — spec § 7.2's pairing requirement is satisfied by construction; the metrics pipeline tests assert this shape (one data point per instrument, empty attributes, shared timestamp).
- `process.uptime`: hand-rolled — no Python OTel package emits it. ~7 lines: one observable gauge with `time.monotonic() - start` callback.

The OTel community library tracks semantic-convention evolution for us; we benefit from upstream maintenance without owning the cross-platform CPU/memory code.

## 13. Defaults out of the box

`init_apitally(app, write_token="...")` with no other kwargs ships:

| Signal | Default |
|---|---|
| Traces (SERVER spans + descendants) | ✓ |
| Metrics (three histograms + process gauges + uptime heartbeat) | ✓ |
| Logs (request-scoped, with trace correlation) | ✓ |
| Exception capture (OTel exception events on SERVER spans) | ✓ |
| `set_consumer(...)` (when user calls it) | ✓ |
| Default redaction patterns | ✓ |
| Default excluded path patterns | ✓ |
| Request body content | ✗ — opt-in |
| Response body content | ✗ — opt-in |
| Request headers | ✗ — opt-in |
| Response headers | ✗ — opt-in |

Default-on logs are a deliberate change from 0.x, where log capture was double opt-in. Rationale and redaction posture in §10.

## 14. Configuration loading

### Precedence (highest to lowest)

1. Explicit kwargs to `init_apitally(...)`.
2. `APITALLY_*` env vars.
3. `OTEL_*` env vars (where semantically equivalent).
4. Apitally defaults.

### Apitally-namespaced env vars

| Env var | Maps to | Notes |
|---|---|---|
| `APITALLY_WRITE_TOKEN` | `write_token` kwarg | Token format `apt_<base62>` per spec. |
| `APITALLY_ENV` | `env` kwarg | Default `"prod"`. |
| `APITALLY_DISABLED` | `disabled` kwarg | Truthy → skip activation. |
| `APITALLY_OTLP_ENDPOINT` | OTLP exporter endpoint override | For local testing only. Default `https://otlp.apitally.io`. No code-level `endpoint=` kwarg. |

### OTel env vars respected (when set and no Apitally-specific override exists)

- `OTEL_SERVICE_NAME` → `service.name` resource attr
- `OTEL_RESOURCE_ATTRIBUTES` → merged into resource (user values win over Apitally defaults for the same key)
- `OTEL_LOG_LEVEL`, `OTEL_PYTHON_LOG_LEVEL` → OTel SDK internal logging
- `OTEL_SDK_DISABLED` → respected (user explicitly disabled OTel)

### OTel env vars ignored

- `OTEL_EXPORTER_OTLP_ENDPOINT` — Apitally endpoint is fixed; use `APITALLY_OTLP_ENDPOINT` if you need to override. Ignoring the OTel one is necessary so users running another OTel backend on a different endpoint don't accidentally redirect Apitally traffic.
- `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `_METRICS_ENDPOINT`, `_LOGS_ENDPOINT` — same reasoning.
- `OTEL_EXPORTER_OTLP_PROTOCOL` — protocol is fixed (HTTP/protobuf bundled).
- `OTEL_EXPORTER_OTLP_HEADERS` — we set the `Authorization: Bearer <write_token>` header ourselves.
- `OTEL_TRACES_SAMPLER` — own-it-all mode passes an explicit `ALWAYS_ON` sampler (§2), so the SDK never reads this var; we filter processor-side (§3).

## 15. Sentry integration

Auto-detect. On `init_apitally(...)`, check whether the Sentry SDK is available. If found, install a hook that reads Sentry's last event id when an exception is captured and writes `apitally.exception.sentry_event_id` to the active SERVER span.

No opt-in flag. The user installed `sentry-sdk` — that's their consent.

There is no published `sentry` extra — a Sentry user already has `sentry-sdk` installed, so an install convenience would serve nobody; the dev-side dependency group covers testing (as in 0.x).

## 16. File structure (rough)

```
apitally/
  __init__.py                       # exports: set_consumer, set_request_attribute, capture_exception, instrument, span (init_apitally lives in framework modules)
  fastapi.py                        # init_apitally(app: FastAPI, ...) - thin glue
  starlette.py                      # init_apitally(app: Starlette, ...) - thin glue
  django.py                         # init_apitally(...) - no app, end of settings.py
  django_ninja.py                   # tiny re-export / glue
  django_rest_framework.py          # tiny re-export / glue
  flask.py                          # init_apitally(app: Flask, ...) - thin glue
  litestar.py                       # ApitallyPlugin(...) for Litestar(plugins=[...]) - thin glue
  blacksheep.py                     # init_apitally(app: BlackSheep, ...) - thin glue
  otel.py                           # instrument(), span(), instrument_<x>() helpers - kept
  shared/                           # shared logic across frameworks
    asgi.py                         # transport-level body capture middleware (ASGI)
    wsgi.py                         # transport-level body capture middleware (WSGI)
    span_processor.py               # filter processor (root-kind decision, inherited; OPTIONS + excluded-request drop, patterns per spec § 6.8)
    log_processor.py                # filter processor for logs (drop without server_span_id)
    metrics.py                      # three request histograms + SystemMetricsInstrumentor (cpu/mem) + process.uptime gauge
    startup.py                      # startup event emission
    consumer.py                     # set_consumer et al., reading the §5 SERVER-span ContextVar (set in span_processor.py on_start)
    redaction.py                    # default + user patterns, applied on body/headers/query
    config.py                       # kwarg/env-var loading, idempotency state
    activation.py                   # configure/activate state machine, os.register_at_fork handlers
    sentry.py                       # auto-detect + event-id hook
```

`apitally/client/` (the legacy 0.x Hub transport, ~1.3k lines) is removed entirely.

## 17. Cross-language posture

Python is the first SDK to ship under v1. Other languages (Node, .NET, Go, etc.) follow on their own timelines.

Consistency where natural: kwarg/option names (`writeToken` / `WriteToken` etc.), env var names (`APITALLY_WRITE_TOKEN`, `APITALLY_DISABLED`, etc.), default behavior (cooperate with existing OTel, off-by-default for body capture, never-break-app error handling).

Function names are **not** aligned across languages — each SDK picks the verb its community expects. Python uses `init_apitally(app, ...)` because `init_`/`setup_`/`configure_` is the Python SDK convention; JS will likely use `useApitally(app, ...)` because `use` is idiomatic there (React, Express); .NET will use `UseApitally(app, ...)` to match the `IApplicationBuilder.UseX` pattern. The shared identifier is `apitally`, not the verb.

Idiomatic design wins where it must (decorators vs. annotations vs. interceptors, dependency model per language ecosystem, framework-adapter mechanics like Django's settings.py). Not a hard constraint.

## 18. Open items / loose ends

Things worth resolving before or during implementation:

- **Testing strategy**. The current SDK has extensive per-framework integration tests (httpx + TestClient against real framework apps). v1 keeps this pattern but assertions are rewritten against OTel-side data (spans, metrics, logs) instead of legacy Hub payloads. Some tests can use OTel's `InMemorySpanExporter` etc. for fast assertions.
- **Migration guide content**. A docs page that's a 1:1 lookup table from 0.x to v1 patterns. Out of scope for this design doc.
