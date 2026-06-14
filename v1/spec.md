# Apitally OTel SDK Specification

Canonical, language-agnostic contract between Apitally client SDKs and the Apitally OTLP ingestion path. Normative keywords MUST/SHOULD/MAY per RFC 2119. Server behavior is stated only where it constrains the SDK; verified against `apitally_cloud/otlp/` and `apitally_cloud/ingester/otlp_*.py`.

## 1. Overview

Apitally SDKs become OpenTelemetry distributions: they configure the official OTel SDK of their language and export OTLP directly to `otlp.apitally.io`. This is a clean break shipped as a new major version: the Hub, `client_id`, and all bespoke transport/payload code are removed. Auth uses a per-app write token. Legacy SDK versions continue working against the Hub; no dual-mode support.

Deferred, no SDK work required: validation errors and server errors (legacy `app_sync` features) are not part of this contract and will be derived server-side from traces later.

## 2. Transport

| | OTLP/HTTP | OTLP/gRPC |
|---|---|---|
| Endpoint | `https://otlp.apitally.io/v1/{traces,metrics,logs}` | `https://otlp.apitally.io:443` (TLS, ALPN h2) |
| Encoding | `Content-Type: application/x-protobuf` only; JSON is rejected with 415 | protobuf |
| Compression | `Content-Encoding: gzip` or `identity`; anything else is rejected with 400 | standard gRPC gzip |
| Max payload | 4 MiB on the wire | 4 MiB |

Decompressed payloads over 16 MiB are dropped at ingest. SDKs SHOULD use their language's default OTLP protocol; both are fully supported.

## 3. Authentication

Every export MUST carry `Authorization: Bearer <write token>` (HTTP header / gRPC metadata). Token format: `apt_` + 24 alphanumeric chars, e.g. `apt_3kPmN9xQv2bR7tH4wZ8yL5cE`; treat as an opaque string. The write token is a write-only ingest credential (Sentry-DSN class), replacing `client_id`. Missing/invalid token: HTTP 401 / gRPC `UNAUTHENTICATED`, not retryable.

## 4. Export headers / metadata

On every export request, the SDK MUST send:

| HTTP header | gRPC metadata | Value |
|---|---|---|
| `Apitally-Env` | `apitally-env` | The SDK's resolved environment, identical to `deployment.environment.name` (default `prod`). Drives real-time online status, stamped at receive time. |

And SHOULD send:

| HTTP header | gRPC metadata | Value |
|---|---|---|
| `Apitally-Message-Id` | `apitally-message-id` | A UUID (v4) generated per export payload. |

The server derives the message id from `Apitally-Message-Id` when it is a valid UUID, falling back to a hash of the payload; duplicate exports with the same id are deduplicated downstream (NATS message dedup, ClickHouse insert dedup). Retries of a failed export MUST resend byte-identical payloads with the same message id.

## 5. Resource attributes

| Attribute | Requirement | Notes |
|---|---|---|
| `service.name` | MUST | App/service name. |
| `service.instance.id` | MUST | Unique per process instance, regenerated on restart (e.g. UUIDv4 at startup). Server falls back to `service.name` if absent, collapsing all instances into one. |
| `deployment.environment.name` | SHOULD | Apitally environment. Server also accepts deprecated `deployment.environment`; defaults to `prod` when absent. Normalized by slugify, max 32 chars (`Production EU` → `production-eu`). MUST match the `Apitally-Env` header value. Environments are auto-created on first sight. |
| `telemetry.distro.name` | MUST | `apitally-py` for Python SDK, `apitally-js` for JavaScript SDK, etc. |
| `telemetry.distro.version` | MUST | SDK version |
| `telemetry.sdk.*` | — | Set automatically by the OTel SDK. |

Server-side instance identity is `uuid5(namespace, "{app_id}:{env}:{service.instance.id}")`; a restart is a new instance.

## 6. Traces

One SERVER span per handled HTTP request; the SERVER span is the request boundary (identified by `kind == SERVER` only, parent may be non-empty when an upstream propagates `traceparent`). Trace/span IDs are standard W3C sizes (16/8 bytes).

### 6.1 SERVER span attributes

The SDK MUST emit stable HTTP semconv. The server also reads old-convention fallbacks (in parentheses) for stock-OTel compatibility.

| Attribute | Notes / server caps |
|---|---|
| `http.request.method` (`http.method`) | Uppercased; max 16. `OPTIONS` requests are not exported (6.5). |
| `http.route` | MUST be the parameterized route template, e.g. `/users/{user_id}`, never the raw path; max 2048. Unmatched requests (e.g. 404s) have no route: leave it unset. The SERVER span is still emitted and recorded as a request log with an empty route. |
| `http.response.status_code` (`http.status_code`) | Valid range 100–599, else stored as 0. |
| `url.scheme`, `server.address`, `url.path`, `url.query` (`http.scheme`, `http.host`, `http.target`) | Concatenated into the display URL: `{scheme}://{host}{path}?{query}`. |
| `http.request.body.size`, `http.response.body.size` | Full body size in bytes, set regardless of body capture. |
| `client.address` (`net.peer.ip`) | Client IP; max 46 chars. Non-IP or private values are discarded; used for GeoIP. |
| `http.request.header.<name>`, `http.response.header.<name>` | Captured headers, semconv list-valued convention; the server stores one name/value pair per array element. Stored on the request log, stripped from span attributes. |

Request timing comes from span `start_time_unix_nano` / `end_time_unix_nano`.

### 6.2 Consumer attributes

Set on the SERVER span when a consumer is identified:

| Attribute | Cap | Fallback read server-side |
|---|---|---|
| `apitally.consumer.identifier` | 128 | `user.id` |
| `apitally.consumer.name` | 64 | `user.full_name`, then `user.name` |
| `apitally.consumer.group` | 64 | none |

Example: `apitally.consumer.identifier="acme-corp"`, `apitally.consumer.name="Acme Corp"`, `apitally.consumer.group="enterprise"`.

### 6.3 Body capture

| Attribute | Value |
|---|---|
| `apitally.request.body` | Request body as string; absent when not captured |
| `apitally.response.body` | Response body as string; absent when not captured |

- Captured only when request/response body logging is enabled and the `Content-Type` matches the allow-list (case-insensitive prefix match, ignoring any `; charset=...`): `application/json`, `application/problem+json`, `application/vnd.api+json`, `application/ld+json`, `application/x-ndjson`, `text/markdown`, `text/plain`. Otherwise the attribute is absent.
- Bodies up to 50 KB (50,000 bytes) MUST be exported intact, never truncated. Larger bodies are not captured; the attribute is set to `<body too large>`.
- Redaction (section 6.7) MUST run before the attribute is set. When a body mask callback drops the body, the attribute is set to `<masked>`.

### 6.4 Exceptions

Unhandled exceptions MUST be recorded as the standard OTel `exception` span event on the SERVER span (`exception.type`, `exception.message`, `exception.stacktrace`; server caps 256 / 2048 / 64 KiB). The last `exception` event wins. With a Sentry integration active, set `apitally.exception.sentry_event_id` on the SERVER span.

### 6.5 Span selection

- The SERVER span (the request boundary, section 6) and its descendants MUST be exported, except for `OPTIONS` requests (CORS preflight) and excluded requests (section 6.8), which MUST NOT be exported.
- A root span of any other kind (background jobs, queue consumers, schedulers) and its descendants MUST NOT be exported.
- A request MUST be exported even when its upstream parent was not sampled; upstream sampling MUST NOT suppress local requests.

### 6.6 Span noise

The SDK MUST NOT export framework-internal `* http send` / `* http receive` INTERNAL spans. The server drops them anyway as a safety net.

### 6.7 Redaction

Redaction MUST run before any query-param, header, or body attribute (6.1, 6.3) is set. Patterns are matched case-insensitively against the parameter, header, or field name (substring, anywhere in the name); a matched value is replaced with `******`. User-supplied patterns are added to the defaults below, never replace them.

| Target | Default name patterns |
|---|---|
| Query params (in `url.query`) | `auth`, `api-?key`, `secret`, `token`, `password`, `pwd` |
| Headers | `auth`, `api-?key`, `secret`, `token`, `cookie` |
| Body fields | `password`, `pwd`, `token`, `secret`, `auth`, `card[-_ ]?number`, `ccv`, `ssn` |

Body fields are matched on object keys; only string values are replaced, nested objects are walked.

### 6.8 Excluded requests

Requests whose path or user agent matches a built-in pattern MUST NOT be recorded as request logs: no SERVER span (and therefore no request-scoped logs) is exported. They are still counted in request metrics (section 7), which exclusion does not affect.

| Target | Default patterns |
|---|---|
| Path | `/_?healthz?$`, `/_?health[-_]?checks?$`, `/_?heart[-_]?beats?$`, `/ping$`, `/ready$`, `/live$`, `/favicon(?:-[\w-]+)?\.(ico\|png\|svg)$`, `/apple-touch-icon(?:-[\w-]+)?\.png$`, `/robots\.txt$`, `/sitemap\.xml$`, `/manifest\.json$`, `/site\.webmanifest$`, `/service-worker\.js$`, `/sw\.js$`, `/\.well-known/` |
| User agent | `health[-_ ]?check`, `microsoft-azure-application-lb`, `googlehc`, `kube-probe` |

Patterns are matched case-insensitively. User-supplied path patterns are added to the defaults; the user-agent list is not configurable.

## 7. Metrics

### 7.1 Request histograms

The histograms' instrumentation scope name MUST be `apitally`. They MUST be exported every 60 s with delta temporality.

| Instrument | Type | Unit |
|---|---|---|
| `http.server.request.duration` | ExponentialHistogram | `s` |
| `http.server.request.body.size` | ExponentialHistogram | `By` |
| `http.server.response.body.size` | ExponentialHistogram | `By` |

- **Histograms MUST be exponential with delta temporality.** The server reads only exponential + delta; explicit-bucket and cumulative histograms are dropped.
- Scale SHOULD be 3 and MUST be within [-2, +6]; the server drops data points outside the range.
- `http.server.request.duration` is the anchor: its data point `count` is the request count, its `start_time_unix_nano` determines the minute bucket, and size data points join to it by identical attribute tuple; a size data point without a matching duration tuple is dropped.
- Record one duration observation per request (seconds) and one observation per body when the size is known (bytes). `OPTIONS` requests and requests with no matched route (empty `http.route`) MUST NOT be recorded; the server drops both as a safety net. Request logs and spans still capture unmatched-route requests (see 6.1).

Data point attributes — these four form the server's aggregation key and MUST be set:

| Attribute | Notes |
|---|---|
| `http.request.method` | e.g. `GET` |
| `http.route` | route template, same value as on the SERVER span |
| `http.response.status_code` | int |
| `apitally.consumer.identifier` | omit when no consumer; server falls back to `user.id`. Same value as on the SERVER span; server strips whitespace and caps at 128. |

Semconv-required attributes (`url.scheme`; `error.type` on failed requests) SHOULD also be set. All non-key attributes are ignored: data points differing only in non-key attributes are merged server-side (counts, sums, and buckets added).

### 7.2 Process gauges

Reported under any instrumentation scope:

| Instrument | Value | Server handling |
|---|---|---|
| `process.cpu.utilization` | 0–1, normalized across available CPUs | stored as percent, clamped to [0, 100] |
| `process.memory.usage` | bytes (RSS-equivalent) | stored as-is |
| `process.uptime` | seconds | value unused; guarantees an export exists each interval |

CPU and memory are paired by timestamp with ≤1 s skew tolerance; a sample is stored only when both exist, so both SHOULD be observed in the same collection cycle. Gauge or sum data points are accepted; `as_double` or `as_int`.

### 7.3 Liveness contract

Every metrics export is a heartbeat: the server writes a liveness sample per resource (stamped with the max data point time, client clock) and marks the env online while the last export is within 180 s — tolerating two missed 60 s exports. Therefore the metrics export loop MUST run unconditionally on its 60 s interval, independent of traffic; `process.uptime` exists to keep exports non-empty when CPU/memory gauges are disabled. Uptime monitoring and alerts depend on this signal.

## 8. Logs

Only request-scoped logs are stored. Every exported LogRecord MUST carry:

- a non-empty `trace_id`, and
- attribute `apitally.request.server_span_id` = lowercase hex of the request's SERVER span id (16 hex chars, e.g. `00f067aa0ba902b7`).

Records missing either are dropped (the startup event in section 9 is the exception). The native `LogRecord.span_id` (the emitting span, typically a child) is stored for waterfall linking; the explicit SERVER span attribute is required because the server computes the request linkage as `xxh3_128(trace_id_bytes + server_span_id_bytes)`, byte-identical to the trace path.

The SDK MUST set this attribute on every log record emitted during a request, covering both OTel-native logs and the language's standard logging bridge.

| LogRecord field | Stored as | Notes |
|---|---|---|
| `time_unix_nano` (fallback `observed_time_unix_nano`) | timestamp | missing both → dropped |
| `body` | message | strings verbatim; structured bodies JSON-encoded; empty → dropped |
| `severity_number` | level | 1–4 `trace`, 5–8 `debug`, 9–12 `info`, 13–16 `warn`, 17–20 `error`, 21–24 `fatal`, 0 → empty |
| scope `name` | logger | SDK SHOULD set the instrumentation scope name to the application logger name (e.g. `myapp.services.billing`) |
| attr `code.file.path` (`code.filepath`) | file | max 4096 |
| attr `code.line.number` (`code.lineno`) | line | valid 1–65535 |

## 9. Startup event

Emitted on the logs signal as a LogRecord under instrumentation scope `apitally`, with `time_unix_nano` set. The startup event is identified by the event name `apitally.app.startup` together with the scope name. The SDK MUST set this name in the LogRecord's native `event_name` field; where the OTel SDK version cannot, the server also accepts it in an `event.name` attribute as a fallback. No `trace_id` or server-span attribute. Body is a JSON string: the payload below serialized to JSON and set as the LogRecord body (a string `AnyValue`), which the server JSON-decodes. The payload:

```json
{
  "framework": "fastapi",
  "versions": {"python": "3.13.2", "fastapi": "0.115.0", "app": "2.3.1"},
  "paths": [
    {"method": "GET", "path": "/users"},
    {"method": "POST", "path": "/users"}
  ],
  "openapi": "{\"openapi\": \"3.1.0\", ...}"
}
```

| Field | Contract |
|---|---|
| `framework` | e.g. `fastapi`, `express`, `gin`, `aspnetcore` |
| `versions` | component → version map; SHOULD include language runtime and framework |
| `paths` | all registered routes; `method` is 2–12 letters/hyphens (uppercased server-side), `path` is the route template, max 2000 chars; entries MAY include `summary`/`description` strings |
| `openapi` | OpenAPI spec as an uncompressed JSON string; MUST be omitted if larger than 4 MB (4,000,000 bytes). When omitted, endpoints are still registered from `paths` (degraded: no spec-derived summaries/descriptions). |

Emit once when the app is ready (routes registered). Identical startup events from many instances are deduplicated server-side.

## 10. Server responses and retry behavior

Success is HTTP 200 / gRPC `OK` with an empty `Export*ServiceResponse` (no `partial_success`). Error bodies are protobuf `google.rpc.Status`. The endpoint publishes payload bytes without parsing them: a 200 means accepted, not validated — malformed protobuf is dropped at ingest.

| Condition | HTTP | gRPC | Retryable |
|---|---|---|---|
| Invalid/missing token | 401 | `UNAUTHENTICATED` | no |
| Quota exhausted (traces only) | 402 | `RESOURCE_EXHAUSTED` (no RetryInfo) | no — drop |
| Rate limit | 429 | `RESOURCE_EXHAUSTED` + `RetryInfo` | yes |
| Wrong content type | 415 | — | no |
| Unsupported content encoding | 400 | — | no |
| Payload > 4 MiB | 413 (ingress) | `RESOURCE_EXHAUSTED` (gRPC default) | no |
| Server overloaded / upstream down | 503 | `UNAVAILABLE` (+ `RetryInfo` 1 s on concurrency cap) | yes — native backoff |

Rate limits: 1800/minute and 200/second per app per signal. The SDK MUST rely on the OTel exporter's native retry/backoff and MUST NOT implement custom retry; per section 4, retried payloads are byte-identical with a stable message id.

## 11. SDK API guidance (non-normative)

Per-language idioms win; this aligns naming and setup UX across SDKs.

- Setup mirrors the legacy SDK: one entry point per framework, e.g. `use_apitally(app, write_token=..., env=...)` — the distro wires exporters, sampler, processors, and instrumentation internally; no OTel knowledge required from the user.
- Config: `write_token` (snake/camel/Pascal per language), also readable from an `APITALLY_WRITE_TOKEN` env var; `env` defaulting to `prod`.
- Consumer API keeps its name: `set_consumer(identifier, name=None, group=None)` / `setConsumer(...)`, writing the section 6.2 attributes.
- Request logging config (body/header capture toggles, masking) keeps existing legacy SDK option names.
- Build on the official OTel SDK and contrib instrumentations of each language; do not reimplement OTLP export.

## 12. Legacy → OTel mapping

| Legacy SDK feature (Hub) | OTel mechanism |
|---|---|
| `client_id` auth | `apt_…` write token (section 3) |
| Request counters + response time/size histograms (`app_sync`) | three `apitally`-scoped histograms (7.1) |
| Request logs (`app_request_log`) | SERVER spans (6) |
| Application logs (inside request log payload) | OTLP logs + `apitally.request.server_span_id` (8) |
| Startup payload: paths, versions, client, OpenAPI (`app_startup`) | startup log event (9) |
| Heartbeat / online status (`app_sync`) | 60 s metrics export + `Apitally-Env` header (7.3, 4) |
| CPU/memory (`app_sync` resources) | `process.cpu.utilization` + `process.memory.usage` gauges (7.2) |
| Consumer registration | `apitally.consumer.*` attributes (6.2) |
| Validation errors, server errors | deferred — server-side derivation from traces; SDKs emit nothing |
