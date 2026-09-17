# Migrating from 0.x to 1.x

This guide is also available in the [Apitally documentation](https://docs.apitally.io/sdk-reference/python/v1/migration).

The Python SDK now uses OpenTelemetry to collect and send metrics, logs, and traces.

> [!WARNING]
> Request logging, tracing, and application log capture are now enabled by default. If you previously used the SDK for metrics only, set `sample_rate=0` to keep that behavior.

## Installation and setup

The updated [setup guides](https://docs.apitally.io/sdk-reference/python/v1/overview#supported-frameworks) provide the installation steps and initialization code for each framework. Follow these to replace your existing SDK integration.

### Write tokens replace client IDs

The SDK now authenticates with a **write token** instead of a client ID. Your existing app's token (`apt_...`) is available under _Setup instructions_ in the [Apitally dashboard](https://app.apitally.io/apps).

Use this token as the `write_token` argument in place of `client_id`, or set the `APITALLY_WRITE_TOKEN` environment variable.

## Configuration changes

The `RequestLoggingConfig` class and `request_logging_config` argument have been removed. Their settings are now keyword arguments passed directly when initializing the SDK, with the option changes listed below.

### Changed options

The following options have been changed:

| Option | Change |
| --- | --- |
| `client_id` | Replaced by `write_token`, which requires a new credential. |
| `capture_logs` | Default changed from `False` to `True`. |
| `log_request_headers` | Renamed to `capture_request_headers`. |
| `log_request_body` | Renamed to `capture_request_body`. |
| `log_response_headers` | Renamed to `capture_response_headers`. |
| `log_response_body` | Renamed to `capture_response_body`. |
| `mask_request_body_callback` | Renamed to `mask_request_body` with new arguments. |
| `mask_response_body_callback` | Renamed to `mask_response_body` with new arguments. |
| `exclude_callback` | Replaced by `sample_on_request` or `sample_on_response` with new arguments and return values. |
| `exclude_paths` | Matches actual request paths instead of matched route patterns. |
| `urlconf` | Renamed to `django_urlconf`. Selects views to track as well as routes and schemas to discover. |
| `include_django_views` | Renamed to `django_include_class_based_views`. Defaults to `False`, tracking only DRF and Ninja views. Enable it to also track class-based and function-based Django views. |

### Removed options

These options are no longer accepted when initializing the SDK:

| Removed option | Migration |
| --- | --- |
| `request_logging_config` | Pass its settings directly as initialization keyword arguments, applying the changes above. Remove its `enabled` flag. |
| `consumer_callback` and `identify_consumer_callback` | Call `apitally.set_consumer(identifier, name=..., group=...)` during request handling instead of returning a consumer from a callback. |
| `enable_request_logging` and `capture_traces` | Previously defaulted to `False`. Request logging and tracing are now enabled by default. Use `sample_rate=0` to disable request logs and traces. |
| `log_query_params` | Query parameters are now always captured. To mask all values, use `mask_query_params=[r".*"]`. |
| `log_exception` | Unhandled exceptions are now always captured in request traces. |
| `openapi_url` | Custom OpenAPI URLs are no longer supported. FastAPI's schema is captured automatically. |
| `filter_openapi_paths` | Schema routes are now automatically filtered from the reported endpoint list. |
| `capture_client_disconnects` | Removed without a replacement. |
| `proxy` | Configure proxies through `HTTPS_PROXY`, `HTTP_PROXY`, and `NO_PROXY` environment variables. |

The [configuration reference](https://docs.apitally.io/sdk-reference/python/v1/configuration) lists all available options.

## Consumer identification

The SDK now provides `apitally.set_consumer()` for all frameworks. The request argument and `ApitallyConsumer` class have been removed.

Call it where the consumer is known, such as in your authentication code:

```python
import apitally

apitally.set_consumer(
    user.identifier,
    name=user.name,  # optional
    group=user.group,  # optional
)
```

This replaces `consumer_callback`, `identify_consumer_callback`, and consumer values assigned to request state. Existing `set_consumer(request, ...)` calls should use the new function without the `request` argument.

## Body masking callbacks

`mask_request_body_callback` and `mask_response_body_callback` are now named `mask_request_body` and `mask_response_body`. Both receive `(span, body)`, rather than request/response dictionaries. The body is passed as `bytes`. Request metadata is available through [`span.attributes`](https://docs.apitally.io/sdk-reference/python/v1/attributes).

For example, a callback that masks bodies for admin routes becomes:

```python
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan


# Before
def mask_request_body(request: dict[str, Any]) -> bytes | None:
    if (request["path"] or "").startswith("/admin/"):
        return None
    return request["body"]


# After
def mask_request_body(span: ReadableSpan, body: bytes) -> bytes | None:
    route = (span.attributes or {}).get("http.route")
    if isinstance(route, str) and route.startswith("/admin/"):
        return None
    return body
```

## Request exclusion

Use sampling callbacks to exclude requests: `sample_on_request(span)` for early decisions based on the request, or `sample_on_response(span)` for decisions based on the response status or consumer.

The callbacks should return `True` to capture the request, and `False` to exclude it. Callbacks can also return a probability as `float` between 0 and 1. Returning `None` preserves a previously made sampling decision.

For example, to capture only error responses:

```python
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan


# Before
def exclude_request(request: dict[str, Any], response: dict[str, Any]) -> bool:
    return response["status_code"] < 400


# After
def sample_on_response(span: ReadableSpan) -> bool:
    status_code = (span.attributes or {}).get("http.response.status_code")
    return isinstance(status_code, int) and status_code >= 400
```

Replace the `exclude_callback` argument with the appropriate sampling callback. Note that captured headers and bodies are not available in sampling callbacks.

Sampling affects request logs and traces, but not metrics.

See [sampling](https://docs.apitally.io/sdk-reference/python/v1/sampling) for details.

### Path exclusions

`exclude_paths` now matches request paths rather than matched route patterns. If a pattern contains route parameters, update it to match concrete values. For example, replace `r"^/users/\{id\}$"` with `r"^/users/[^/]+$"` to match `/users/123`.

## Existing OpenTelemetry setups

If you already have a global OpenTelemetry SDK `TracerProvider`, the SDK automatically adds its span processor. No manual registration is required.

Review these settings when upgrading:

- **Sampling:** Previously, your provider's sampler affected traces but not Apitally's request logs. It now affects both. Check that its sampling rate provides the request log coverage you want. Metrics remain unsampled.
- **Environment:** Previously, Apitally used its configured `env`. Your provider's `deployment.environment.name` now takes precedence when set. Align conflicting values or omit `env` to use the provider's value.
