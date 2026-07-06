# Migrating from 0.x to 1.x

Version 1.0 of the Apitally SDK for Python is a rewrite. It is now an [OpenTelemetry](https://opentelemetry.io) distribution: the SDK builds on the community OTel instrumentations for each framework and sends data to Apitally via OTLP. Setup is a single line, the credential has changed, and several 0.x options were renamed or removed. This guide maps every 0.x pattern to its 1.x equivalent.

> [!WARNING]
> **Application logs are now captured and sent to Apitally by default**, and log message content is *not* redacted. In 0.x, log capture was opt-in and required request logging to be enabled. If your application logs sensitive data, sanitize it at the logging source before upgrading, or opt out with `capture_logs=False`.

## New credential: write token

The `client_id` is gone. 1.x authenticates with a *write token* (format `apt_...`), which you can find in the Apitally dashboard. Pass it as the `write_token` argument or set the `APITALLY_WRITE_TOKEN` environment variable.

## Setup

The `ApitallyMiddleware` class is gone. Setup is now a call to `init_apitally(...)`, except for Litestar, which keeps its plugin (Litestar plugins must be passed at construction, so a plugin remains the right shape there).

| Framework | 0.x | 1.x |
| --- | --- | --- |
| FastAPI | `app.add_middleware(ApitallyMiddleware, client_id="...")` | `init_apitally(app, write_token="...")` |
| Starlette | `app.add_middleware(ApitallyMiddleware, client_id="...")` | `init_apitally(app, write_token="...")` |
| Flask | `app.wsgi_app = ApitallyMiddleware(app, client_id="...")` | `init_apitally(app, write_token="...")` |
| Django | `"apitally.django.ApitallyMiddleware"` in `MIDDLEWARE` + `APITALLY_MIDDLEWARE` setting | `init_apitally(write_token="...")` at the *end* of `settings.py` |
| Litestar | `Litestar(plugins=[ApitallyPlugin(client_id="...")])` | `Litestar(plugins=[ApitallyPlugin(write_token="...")])` |
| BlackSheep | `use_apitally(app, client_id="...")` | `init_apitally(app, write_token="...")` |

Import `init_apitally` from the framework-specific module, e.g. `from apitally.fastapi import init_apitally`.

**Django users**: remove the `"apitally.django.ApitallyMiddleware"` entry from `MIDDLEWARE` and the `APITALLY_MIDDLEWARE` settings dict. 1.x inserts its own middleware automatically when you call `init_apitally(...)`. The old class no longer exists, so a stale `MIDDLEWARE` entry makes Django fail at startup. Call `init_apitally(...)` at the very end of `settings.py`, after `MIDDLEWARE` is defined.

## Option lookup

All options move to keyword arguments of `init_apitally(...)` (or `ApitallyPlugin(...)` for Litestar). The `RequestLoggingConfig` class and the `request_logging_config` argument are gone; its fields are now flat keyword arguments.

| 0.x | 1.x |
| --- | --- |
| `client_id` | `write_token` (new credential, from the Apitally dashboard, or `APITALLY_WRITE_TOKEN`) |
| `env` (default `"dev"`) | `env` (default changed to `"prod"`, or `APITALLY_ENV`) |
| `app_version` | `app_version` (unchanged) |
| `openapi_url` (FastAPI) | `openapi_url` (unchanged) |
| `openapi_url` (Starlette) | Removed. Plain Starlette apps report route templates only. |
| `consumer_callback` | Removed. Call `set_consumer(...)` from your auth middleware or dependencies (see below). |
| `identify_consumer_callback` | Removed. Same replacement: `set_consumer(...)`. |
| `set_consumer(request, ...)` / `request.state.apitally_consumer` / `request.apitally_consumer` | `from apitally import set_consumer; set_consumer(identifier, name=..., group=...)`. No request object needed. |
| `ApitallyConsumer(identifier, name=..., group=...)` | Removed. Pass the values directly to `set_consumer(...)`. |
| `proxy` | Removed. The exporter honors the standard `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` environment variables. |
| `capture_client_disconnects` | Removed, no replacement. |
| `filter_openapi_paths` (Litestar) | Removed. OpenAPI schema routes are always filtered from the reported endpoint list. |
| `urlconf` (Django) | `urlconf` (unchanged) |
| `include_django_views` (Django) | Kept, but now only affects the reported endpoint list. |
| `request_logging_config=RequestLoggingConfig(...)` | Removed. Use the flat keyword arguments below. |
| `enable_request_logging` | Removed. Request logs are always captured. |
| `log_query_params` | Removed. Query params are always captured, with masking applied. |
| `log_request_headers` | `log_request_headers` (unchanged, default `False`) |
| `log_request_body` | `log_request_body` (unchanged, default `False`) |
| `log_response_headers` | `log_response_headers` (unchanged, default `True`) |
| `log_response_body` | `log_response_body` (unchanged, default `False`) |
| `log_exception` | Removed. Exceptions are always captured as OpenTelemetry exception events. |
| `capture_logs` | `capture_logs`, now a top-level argument and **default `True`** (see the warning at the top). |
| `capture_traces` | Removed. Traces are a core signal in 1.x and always captured. |
| `mask_query_params` | `mask_query_params` (unchanged) |
| `mask_headers` | `mask_headers` (unchanged) |
| `mask_body_fields` | `mask_body_fields` (unchanged) |
| `mask_request_body_callback` | `mask_request_body`, new signature (see below) |
| `mask_response_body_callback` | `mask_response_body`, new signature (see below) |
| `exclude_paths` | `exclude_paths` (unchanged) |
| `exclude_callback` | Removed. Replaced by the sampling API: `sample_rate`, `sample_on_request` / `sample_on_response` (see below). |
| `apitally.client.*` imports | Removed. The Hub transport is replaced by OTLP export; there is no public API under `apitally.client`. |
| Validation and server error capture | No SDK-side API anymore. Apitally derives these server-side from standard OpenTelemetry exception events on traces. |

## Consumers

Instead of returning a consumer from a callback, call `set_consumer(...)` from wherever you authenticate the request, for example an auth middleware or a FastAPI dependency:

```python
from apitally import set_consumer

def get_current_user(...):
    user = ...
    set_consumer(user.identifier, name=user.name, group=user.group)
    return user
```

## Sampling and excluding requests

The `exclude_callback(request, response)` callback is replaced by request sampling. **Note the polarity flip**: the old callback returned `True` to exclude a request, the new callbacks return what to *keep* — `True` / `1.0` means always capture, `False` / `0.0` means never, a float in between is the fraction of matching requests to capture, and `None` means no opinion. Metrics always count every request, regardless of sampling.

- `sample_rate` sets the static fraction of requests captured as traces with their logs (default `1.0`).
- `sample_on_request` refines it per request at span start. Nothing about a sampled-out request is ever transmitted, and capture work such as body buffering and masking is skipped, making this (and `sample_rate`) the lever for volume and overhead. Returning `None` falls back to `sample_rate`.
- `sample_on_response` decides at span end, so it can see the response status and any attributes you set via `set_request_attribute(...)`. It is quota-safe: the SDK holds a request's spans and logs in memory until this decision (up to 1,000 of each per request), so a request dropped here transmits nothing and consumes no quota. Capture work still runs for such requests — use request-stage sampling to skip that overhead. Returning `None` leaves the request-stage decision standing. It also cannot rescue a request already dropped at the request stage: the overall capture probability is the minimum of the two stages, so a response-stage boost like the example below only applies to requests that survived `sample_rate` / `sample_on_request`.

Both callbacks operate on the OpenTelemetry attributes of the request's SERVER span:

```python
def sample_on_request(span):
    if str(span.attributes.get("url.path", "")).startswith("/internal/"):
        return False  # never capture, like exclude_callback returning True
    return None  # follow sample_rate

def sample_on_response(span):
    if (span.attributes.get("http.response.status_code") or 0) >= 500:
        return True  # always capture server errors
    return 0.05  # capture 5% of healthy responses

init_apitally(
    app,
    write_token="your-write-token",
    sample_on_request=sample_on_request,
    sample_on_response=sample_on_response,
)
```

Sampling decisions are deterministic per trace ID, so services sampling at the same rate capture the same traces.

## Masking request and response bodies

`mask_request_body_callback` and `mask_response_body_callback` are renamed to `mask_request_body` and `mask_response_body`. They no longer receive request/response dicts; the new signature is `(span, body: bytes) -> bytes | None`, where `span` is the request's SERVER span. Returning `None` replaces the captured body with `[REDACTED]`.

```python
def mask_request_body(span, body):
    if span.attributes.get("url.path") == "/users":
        return None  # body is replaced with [REDACTED]
    return body
```

## Using Apitally alongside an existing OpenTelemetry setup

If your application already has an OpenTelemetry `TracerProvider` configured (e.g. for Datadog, Honeycomb, or your own collector), Apitally attaches to it instead of replacing it. Your existing pipeline is unaffected. A few things to be aware of in this mode:

- The order of `init_apitally(...)` and your other OpenTelemetry setup does not matter, as long as your `TracerProvider` is registered before the application starts serving requests. A provider registered after startup is not picked up.
- Your sampler applies. If it drops requests (e.g. `TraceIdRatioBased`), request logs in Apitally follow your sampling rate; Apitally's own `sample_rate` applies on top, to the requests your sampler keeps. Metrics are recorded independently of sampling and stay complete.
- Your span attribute limits apply. A limit below 65,536 (e.g. via `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT`) can truncate captured request/response bodies. The SDK logs a warning when it detects this.

## Version floors

- The minimum supported Litestar version is now 2.24 (was 2.4).
- The minimum supported Django version is now 3.2 (was 2.2), and Django REST Framework follows at 3.12 (was 3.10).
- The minimum supported BlackSheep version is now 2.6.1 (was 2.0).
- All other framework version floors are unchanged, and Python 3.10+ is still required.
