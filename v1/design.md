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

- **No existing OTel**: install our own `TracerProvider` with our OTLP `BatchSpanProcessor`, resource, and span limits.
- **OTel TracerProvider already configured** (e.g., user has Datadog, Honeycomb, or their own collector): don't replace it. Attach our OTLP `BatchSpanProcessor` additively to the existing `TracerProvider`.
- **MeterProvider and LoggerProvider**: always construct our own, regardless of what the user has.

### Attribute length limit

In own-it-all mode, construct the `TracerProvider` with `SpanLimits(max_attribute_length=65_536)`. The OTel default is unlimited, but if the user has set `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT` in their environment, that value applies unless we pass an explicit numeric override. 65 KiB comfortably fits a 50,000-byte body (per spec § 6.3).

### Environment resolution and the `Apitally-Env` header

The `Apitally-Env` header (spec § 4) MUST match `deployment.environment.name` on the Resource. Resolve once at `init_apitally(...)` and pass to our OTLP exporter's `headers={...}`.

- **Own-it-all mode**: env = `env=` kwarg / `APITALLY_ENV` / `"prod"`. Set both `deployment.environment.name` on our Resource AND `Apitally-Env` on the exporter from this single value.
- **Cooperative mode**: env = user's `deployment.environment.name` (from `user_provider.resource.attributes`) if present, else `env=` kwarg / `APITALLY_ENV` / `"prod"`. Use this value for `Apitally-Env` on our exporter. We do not modify the user's Resource.
- **Conflict**: if cooperative-mode AND the user's Resource has `deployment.environment.name` AND the caller passed `env=` with a different value, log a warning and use the Resource value. Users who want a separate Apitally env must either change their OTel env or use own-it-all mode.

## 3. Span filtering — single mechanism

Apitally only ingests request-rooted traces. Non-request spans (background workers, schedulers, queue consumers) must not be exported to Apitally.

One mechanism handles this in both own-it-all and cooperative modes: a `SpanProcessor` wrapper around our OTLP exporter.

- On `on_start`: read the parent's `span_id` from `span.parent` (a `SpanContext`), then look up its `(keep, server_span_id)` in the in-flight map. For root spans (`span.parent is None`): SERVER → `(True, own_span_id)`; otherwise `(False, None)`. Children inherit the parent's entry.
- On `on_end`: if `keep` is true, forward to the wrapped batch processor; otherwise drop. Pop the span_id from the in-flight map.
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
| BlackSheep | `opentelemetry-instrumentation-asgi` (generic fallback) | No community-specific instrumentor exists; generic ASGI is sufficient. Thin BlackSheep-specific hook resolves `http.route`. |

The stock instrumentor produces the SERVER span and sets standard HTTP attributes. Our framework-specific glue adds:
- Body capture (request + response) — see §6
- Consumer attribute setter (`set_consumer(...)`)
- Histogram recording at request end (the three exponential histograms from the spec)
- Startup event (routes + OpenAPI) emission
- Sentry event-id linkage (when Sentry is detected)

Every ASGI integration is configured with `exclude_spans=["receive", "send"]` — FastAPI, Starlette, and the generic ASGI fallback for BlackSheep directly, and Litestar via its OTel plugin's `OpenTelemetryConfig`. By default they emit one INTERNAL span per ASGI `receive()` and `send()` call — pure noise per spec § 6.6.

## 5. Public API

### Setup

`init_apitally(app, ...)` per framework. Regular function call with explicit typed kwargs; IDE autocomplete and type hints work properly. Replaces today's `app.add_middleware(ApitallyMiddleware, ...)` pattern.

For Django: `init_apitally(...)` with no `app` argument, called at the **end of `settings.py`**. This is the canonical Django integration location (mirroring Sentry and Logfire), placed after `MIDDLEWARE` is defined so `DjangoInstrumentor` can prepend its middleware safely.

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
| Body/header capture toggles, redaction extras, excluded paths, etc. | (per legacy SDK option names) | (per spec) | Off by default — see §6 |

### Top-level public surface

| Symbol | Purpose |
|---|---|
| `init_apitally(...)` | Per-framework setup. |
| `set_consumer(identifier, name=None, group=None)` | Sets `apitally.consumer.*` on the active SERVER span. |
| `set_request_attribute(key, value)` | New in v1. Sets arbitrary attribute on active SERVER span. |
| `capture_exception(exc)` | New in v1. Records an exception event on active SERVER span. |
| `instrument(func)` | Decorator. Wraps a function in a span. Kept from existing `otel.py`. |
| `span(name, attributes=None)` | Context manager. Kept from existing `otel.py`. |
| `instrument_httpx`, `instrument_sqlalchemy`, etc. | Lazy-import thin wrappers around community instrumentors. Kept as-is. |

### Removed

- All of `apitally.client.*` (Hub transport, threading/asyncio variants, message queues, validation/server error capture, sentry bridge).
- `client_id` everywhere — replaced by `write_token`.
- `ApitallyMiddleware` class — replaced by `init_apitally(...)`.
- Separate validation/server error capture API — cloud derives these server-side from standard OTel exception events on traces.

## 6. Body and header capture

### Default: OFF

Body capture and header capture are **disabled by default**. Privacy posture matches the legacy SDK — users explicitly opt in to capture payload content. Defaults give a strong product without payload data: traces, metrics, logs, exceptions, consumer attribution.

### Implementation: transport-level

One ASGI middleware covers all ASGI frameworks (FastAPI, Starlette, Litestar, BlackSheep, Django-ASGI). One WSGI middleware covers all WSGI frameworks (Flask, Django-WSGI). Per-framework adapters wire up the right transport middleware.

Reading the body is a transport concern, not a framework concern. Per-framework duplication is what made `client/request_logging.py` 573 lines; transport-level is the path to "simple, lightweight, minimal SDK code."

Capture mechanics (per spec section 6.3):
- Read up to 50 KiB + 1 bytes. Within limit → capture. Over limit → skip; set attribute to `<body too large>`.
- MIME allowlist (spec § 6.3): `application/json`, `application/problem+json`, `application/vnd.api+json`, `application/ld+json`, `application/x-ndjson`, `text/plain`. Outside list → no attribute set.
- Redaction order: read → MIME filter → size check → parse JSON if applicable → redact → re-serialize → set `apitally.request.body` / `apitally.response.body` on the active SERVER span.
- Default redaction patterns per spec § 6.7. User-supplied patterns add to defaults, never replace.

## 7. Activation model

Setup happens at app start; activation (network threads, heartbeat, startup event) is gated to ensure correctness in fork-based servers and non-server contexts.

### Configure phase (eager, all frameworks)

`init_apitally(...)` does:
- Register OTel providers (or attach to existing).
- Install stock instrumentor.
- Attach our ASGI/WSGI middleware to the app.
- Connect Django's `request_started` signal (Django only).
- Build meter, histograms, observable gauges.
- No threads, no network, no fork-unsafe objects.

### Activate phase

- **Non-Django frameworks**: activate **eagerly** inside `init_apitally(app, ...)`. The user calling it with an `app` is itself proof we're in a server bootstrap. Exporters attached, heartbeat starts, startup event fires.
- **Django**: activate on first `django.core.signals.request_started`. settings.py can't distinguish "about to serve HTTP" from "about to run `migrate`" or "Celery worker bootstrapping," so the signal is the only robust gate. New Relic uses the same pattern.

### Post-fork re-activation (all frameworks)

On every middleware request entry, check `os.getpid() != _activated_pid`. If different, the process forked since activation (gunicorn `--preload`, uWSGI without `lazy-apps`, etc.). Drop the stale exporter references, re-activate in the new PID. One PID compare per request hot path.

This sidesteps every detection-edge-case at once: management commands, Celery workers, pytest, REPLs, autoreloader parents — none of them ever trigger `request_started` or our middleware, so they configure but never activate.

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

Apitally is always loud in logs about what's wrong (no silent failures, no debug-level swallowing of problems) but never raises. `try/except Exception` (not `BaseException`) wraps every entry point.

Debuggability requirement: users must know if something is broken. Visible errors in logs. The SDK doesn't quietly do nothing.

## 10. Logs

Auto-install OTel's `LoggingHandler` on the root logger as part of `init_apitally(...)`. Captures stdlib `logging` output and routes it through the LoggerProvider.

- Level: `NOTSET` — capture everything that makes it through user-configured per-logger thresholds.
- Opt out via `capture_logs=False` kwarg for users with strong opinions about their logging stack.
- Stock `LoggingHandler` already stamps `trace_id` and `span_id` on every record from the active span at emit time.
- We additionally need `apitally.request.server_span_id` on every request-scoped record — the cloud ingester reads it directly (spec § 9) to derive `request_uuid = hash(trace_id + server_span_id)` and join logs to requests. The handler's `span_id` is the immediate active span (often an INTERNAL child of SERVER) and cannot be used.
- Mechanism: extend the in-flight map maintained by the `ApitallySpanProcessor` (see §3) from `span_id → keep_bool` to `span_id → (keep_bool, server_span_id)`. On `on_start`: SERVER root → `(True, own_span_id)`; other root → `(False, None)`; child → inherit parent's entry. On `on_end`: pop.
- Our `LogRecordProcessor.on_emit(...)` reads `record.span_id`, looks it up in the same in-flight map, and writes `apitally.request.server_span_id = server_span_id.hex()` onto the record.
- The same processor drops any record without an `apitally.request.server_span_id` resolution, UNLESS its instrumentation scope is `apitally` (preserves the startup event, which is emitted on the logs signal under scope `apitally` per spec § 9 with no request context).
- Net effect: only request-scoped logs reach the OTLP export, and every one carries the correct SERVER span id regardless of how deeply nested the emitting span was. No ContextVar. No dependency on middleware ordering.

## 11. Dependencies

### Required (always installed)

- `opentelemetry-api`
- `opentelemetry-sdk`
- `opentelemetry-exporter-otlp-proto-http` — HTTP/protobuf transport only; gRPC NOT bundled (see below)
- `opentelemetry-instrumentation` (base class for instrumentors)
- `opentelemetry-instrumentation-logging` (logging bridge — see §10)
- `opentelemetry-instrumentation-system-metrics` (process gauges; pulls `psutil` transitively)

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

One forced change: Starlette floor moves from 0.26 → 0.35 because `opentelemetry-instrumentation-starlette` requires that minimum. Others stay where they are.

## 12. Process gauges

Per spec section 7.2: three gauges required (`process.cpu.utilization`, `process.memory.usage`, `process.uptime`).

- `process.cpu.utilization` + `process.memory.usage`: via `opentelemetry-instrumentation-system-metrics`, configured to emit ONLY those two metrics (the library defaults to ~30 metrics including disk/network/GC; we restrict via the `config=` constructor arg).
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
- `OTEL_TRACES_SAMPLER` — we filter processor-side, not via a global sampler.

## 15. Sentry integration

Auto-detect. On `init_apitally(...)`, check whether the Sentry SDK is available. If found, install a hook that reads Sentry's last event id when an exception is captured and writes `apitally.exception.sentry_event_id` to the active SERVER span.

No opt-in flag. The user installed `sentry-sdk` — that's their consent.

The `apitally[sentry]` extra exists as an install convenience (`sentry-sdk>=2.2.0`) but isn't required for auto-detection to work.

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
  litestar.py                       # init_apitally(app: Litestar, ...) - thin glue
  blacksheep.py                     # init_apitally(app: BlackSheep, ...) - thin glue
  otel.py                           # instrument(), span(), instrument_<x>() helpers - kept
  shared/                           # shared logic across frameworks
    asgi.py                         # transport-level body capture middleware (ASGI)
    wsgi.py                         # transport-level body capture middleware (WSGI)
    span_processor.py               # filter processor (root-kind decision, inherited)
    log_processor.py                # filter processor for logs (drop without server_span_id)
    metrics.py                      # three request histograms + SystemMetricsInstrumentor (cpu/mem) + process.uptime gauge
    startup.py                      # startup event emission
    consumer.py                     # set_consumer + ContextVar
    redaction.py                    # default + user patterns, applied on body/headers/query
    config.py                       # kwarg/env-var loading, idempotency state
    activation.py                   # configure/activate state machine, post-fork PID check
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
