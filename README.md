<p align="center">
  <a href="https://apitally.io" target="_blank">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://assets.apitally.io/logos/logo-horizontal-new-dark.png">
      <source media="(prefers-color-scheme: light)" srcset="https://assets.apitally.io/logos/logo-horizontal-new-light.png">
      <img alt="Apitally logo" src="https://assets.apitally.io/logos/logo-horizontal-new-light.png" width="220">
    </picture>
  </a>
</p>
<p align="center"><b>API monitoring & analytics made simple</b></p>
<p align="center" style="color: #ccc;">Metrics, logs, traces, and alerts for your APIs — with just a few lines of code.</p>
<br>
<p>
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://assets.apitally.io/screenshots/overview-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="https://assets.apitally.io/screenshots/overview-light.png">
  <img alt="Apitally dashboard" src="https://assets.apitally.io/screenshots/overview-light.png">
</picture>
</p>
<br>

# Apitally SDK for Python

[![Tests](https://github.com/apitally/apitally-py/actions/workflows/tests.yaml/badge.svg?event=push)](https://github.com/apitally/apitally-py/actions)
[![Codecov](https://codecov.io/gh/apitally/apitally-py/graph/badge.svg?token=UNLYBY4Y3V)](https://codecov.io/gh/apitally/apitally-py)
[![PyPI](https://img.shields.io/pypi/v/apitally?logo=pypi&logoColor=white&color=%23006dad)](https://pypi.org/project/apitally/)

Apitally is a simple API monitoring and analytics tool that makes it easy to understand API usage, monitor performance, and troubleshoot issues.
Get started in minutes by just adding a few lines of code. No infrastructure changes required, no dashboards to build.

The SDK is an [OpenTelemetry](https://opentelemetry.io) distribution and works alongside an existing OpenTelemetry setup.

Learn more about Apitally on our 🌎 [website](https://apitally.io) or check out the 📚 [documentation](https://docs.apitally.io).

> [!IMPORTANT]
> **Upgrading from 0.x?** Version 1.0 is a full rewrite with a new setup API. See the [migration guide](MIGRATION.md) for how to upgrade.

## Key features

- **API analytics**: Traffic, error and performance metrics for your API, each endpoint, and per API consumer. Drill down from metrics to individual API requests.
- **Request logs and traces**: Every request as a searchable log entry, with optional capture of headers and request/response bodies. Requests are exported as OpenTelemetry spans, including spans from any other instrumentations you have.
- **Application logs**: Logs written via the standard `logging` module and Loguru are captured automatically and correlated with the requests they belong to.
- **Error tracking**: Validation errors and exceptions with stack traces for server errors, automatically linked to Sentry events if you use Sentry.
- **Server metrics**: CPU and memory usage of your app's processes.
- **API monitoring & alerts**: Get notified if something isn't right using custom alerts, synthetic uptime checks and heartbeat monitoring. Alert notifications can be delivered via email, Slack and Microsoft Teams.

## Supported frameworks

The SDK supports **Python** `>= 3.10`.

| Framework                                                                    | Supported versions | Setup guide                                                         |
| ---------------------------------------------------------------------------- | ------------------ | ------------------------------------------------------------------- |
| [**FastAPI**](https://github.com/fastapi/fastapi) \*                         | `>=0.108.0`        | [Link](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/fastapi)               |
| [**Django REST Framework**](https://github.com/encode/django-rest-framework) | `>=3.12.0`         | [Link](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/django-rest-framework) |
| [**Django Ninja**](https://github.com/vitalik/django-ninja)                  | `>=1.0.0`          | [Link](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/django-ninja)          |
| [**Flask**](https://github.com/pallets/flask)                                | `>=2.0.0`          | [Link](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/flask)                 |
| [**Starlette**](https://github.com/encode/starlette)                         | `>=0.29.0`         | [Link](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/starlette)             |
| [**Litestar**](https://github.com/litestar-org/litestar)                     | `>=2.24.0`         | [Link](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/litestar)              |
| [**BlackSheep**](https://github.com/Neoteroi/blacksheep)                     | `>=2.6.1`          | [Link](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/blacksheep)            |

\* For FastAPI on Cloudflare Workers use our [Python Serverless SDK](https://github.com/apitally/apitally-py-serverless) instead.

Apitally also supports many other web frameworks in [JavaScript](https://github.com/apitally/apitally-js), [Go](https://github.com/apitally/apitally-go) and [.NET](https://github.com/apitally/apitally-dotnet) via our other SDKs.

## Getting started

If you don't have an Apitally account yet, first [sign up here](https://app.apitally.io/?signup). Then create an app in the Apitally dashboard. You'll see detailed setup instructions with code snippets you can copy and paste. These also include your write token.

See the [SDK reference](https://docs.apitally.io/sdk-reference/python/v1/configuration) for all available configuration options, including how to mask sensitive data, capture request and response payloads, and more.

### FastAPI

Install the SDK with the `fastapi` extra:

```bash
pip install "apitally[fastapi]"
```

Then call `apitally.init()` right after creating the app:

```python
import apitally
from fastapi import FastAPI

app = FastAPI()
apitally.init(app, write_token="your-write-token", env="dev")
```

For further instructions, see our [setup guide for FastAPI](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/fastapi).

### Django

Install the SDK with the `django` extra:

```bash
pip install "apitally[django]"
```

Then call `apitally.init()` at the *end* of your `settings.py` module, after `MIDDLEWARE` is defined:

```python
# settings.py
import apitally

MIDDLEWARE = [
    # Your middleware ...
]

# ... at the very end of the file:
apitally.init(write_token="your-write-token", env="dev")
```

For further instructions, see our [setup guide for Django](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/django).

### Flask

Install the SDK with the `flask` extra:

```bash
pip install "apitally[flask]"
```

Then call `apitally.init()` after creating the app and after wrapping `app.wsgi_app` with any WSGI middleware:

```python
import apitally
from flask import Flask

app = Flask(__name__)
apitally.init(app, write_token="your-write-token", env="dev")
```

For further instructions, see our [setup guide for Flask](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/flask).

### Starlette

Install the SDK with the `starlette` extra:

```bash
pip install "apitally[starlette]"
```

Then call `apitally.init()` right after creating the app:

```python
import apitally
from starlette.applications import Starlette

app = Starlette(routes=[...])
apitally.init(app, write_token="your-write-token", env="dev")
```

For further instructions, see our [setup guide for Starlette](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/starlette).

### Litestar

Install the SDK with the `litestar` extra:

```bash
pip install "apitally[litestar]"
```

Then add `ApitallyPlugin` to your app's `plugins`:

```python
from litestar import Litestar
from apitally.litestar import ApitallyPlugin

app = Litestar(
    route_handlers=[...],
    plugins=[ApitallyPlugin(write_token="your-write-token", env="dev")],
)
```

For further instructions, see our [setup guide for Litestar](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/litestar).

### BlackSheep

Install the SDK with the `blacksheep` extra:

```bash
pip install "apitally[blacksheep]"
```

Then call `apitally.init()` right after creating the app:

```python
import apitally
from blacksheep import Application

app = Application()
apitally.init(app, write_token="your-write-token", env="dev")
```

For further instructions, see our [setup guide for BlackSheep](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/blacksheep).

## Configuration

Pass configuration options as keyword arguments to `apitally.init()`, or to `ApitallyPlugin` for Litestar. See the [SDK reference](https://docs.apitally.io/sdk-reference/python/v1/configuration) for all available options.

### Environment variables

To keep configuration out of your code, set these environment variables instead of passing the matching arguments:

- `APITALLY_WRITE_TOKEN`: the write token (`write_token`)
- `APITALLY_ENV`: the environment name (`env`), defaults to `dev`
- `APITALLY_DISABLED`: set to `true` to disable Apitally (`disabled`)

### Request and response data

Apitally captures response headers by default. To also capture request headers and request and response bodies, pass these arguments:

```python
apitally.init(
    app,
    write_token="your-write-token",
    env="dev",
    capture_request_headers=True,
    capture_request_body=True,
    capture_response_body=True,
)
```

Apitally automatically masks common sensitive values, such as `password` fields and the `Authorization` header. To mask additional values, pass regular expressions to `mask_query_params`, `mask_headers`, and `mask_body_fields`. See our [documentation on masking](https://docs.apitally.io/sdk-reference/python/v1/masking) for details.

### Exclusions and sampling

Apitally automatically excludes common health checks and static assets, such as `/healthz` and `/robots.txt`, from logs and traces. To exclude additional paths, pass regular expressions to `exclude_paths`.

To capture logs and traces for only a fraction of the remaining requests, set `sample_rate`, for example to `0.1` for 10%. To decide for each request individually, pass a callback to `sample_on_request` or `sample_on_response`.

Metrics always include all requests. See our [documentation on sampling](https://docs.apitally.io/sdk-reference/python/v1/sampling) for details.

### Application logs

Apitally captures logs written with the standard `logging` module and Loguru, and correlates them with the requests they belong to. To mask or drop individual log records, pass a callback to `mask_log_record`. To turn off log capture, set `capture_logs=False`.

See our [documentation on masking](https://docs.apitally.io/sdk-reference/python/v1/masking#log-record-masking-callback) for details.

## Adding context to requests

The following functions add information that only your application knows to the current request. Call them while handling a request, for example in an endpoint, middleware, or authentication code.

To associate the request with the API consumer making it, call `set_consumer`. Only the identifier is required.

```python
apitally.set_consumer(
    user.identifier,
    name=user.name,
    group=user.group,
    attributes={"plan": user.plan},
)
```

To attach custom data to the request's trace, call `set_request_attribute`. Sampling and masking callbacks can also read these attributes.

```python
apitally.set_request_attribute("tenant.id", tenant_id)
```

To capture an exception that your code handles itself, call `capture_exception`. Unhandled exceptions are captured automatically.

```python
try:
    process_order()
except Exception as exc:
    apitally.capture_exception(exc)
```

See our documentation on [custom attributes](https://docs.apitally.io/sdk-reference/python/v1/attributes#set-custom-attributes) and [handled exceptions](https://docs.apitally.io/sdk-reference/python/v1/tracing#capture-handled-exceptions) for details.

## Existing OpenTelemetry setup

If your app already uses OpenTelemetry, set up your tracer provider and any framework instrumentation, such as `FastAPIInstrumentor`, before initializing Apitally. Apitally adds its span processor to your tracer provider and keeps your existing exporters.

Your tracer provider's sampler also applies to Apitally. Requests it drops have no request logs or traces in Apitally, but still count in metrics.

## Trusted proxies

If your app runs behind a reverse proxy or load balancer, configure trusted proxies in your server or framework, for example with Uvicorn's `--forwarded-allow-ips` option or Werkzeug's `ProxyFix` middleware. Apitally records the client IP your framework reports and uses it for GeoIP.

## Getting help

If you need help please [create a new discussion](https://github.com/orgs/apitally/discussions/categories/q-a) on GitHub or email us at [support@apitally.io](mailto:support@apitally.io). We'll get back to you as soon as possible.

## License

This library is licensed under the terms of the [MIT license](LICENSE).
