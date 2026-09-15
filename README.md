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
> **Upgrading from 0.x?** Version 1.0 is a full rewrite with a new setup API. See the [migration guide](MIGRATION.md) for a full 0.x to 1.x mapping.

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
| [**Flask**](https://github.com/pallets/flask)                                | `>=2.0.0`          | [Link](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/flask)                 |
| [**Django**](https://github.com/django/django)                               | `>=3.2`            | [Link](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/django)                |
| [**Django REST Framework**](https://github.com/encode/django-rest-framework) | `>=3.12.0`         | [Link](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/django-rest-framework) |
| [**Django Ninja**](https://github.com/vitalik/django-ninja)                  | `>=1.0.0`          | [Link](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/django-ninja)          |
| [**Starlette**](https://github.com/encode/starlette)                         | `>=0.29.0`         | [Link](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/starlette)             |
| [**Litestar**](https://github.com/litestar-org/litestar)                     | `>=2.24.0`         | [Link](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/litestar)              |
| [**BlackSheep**](https://github.com/Neoteroi/blacksheep)                     | `>=2.6.1`          | [Link](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/blacksheep)            |

\* For FastAPI on Cloudflare Workers use our [Python Serverless SDK](https://github.com/apitally/apitally-py-serverless) instead.

Apitally also supports many other web frameworks in [JavaScript](https://github.com/apitally/apitally-js), [Go](https://github.com/apitally/apitally-go), [.NET](https://github.com/apitally/apitally-dotnet) and [Java](https://github.com/apitally/apitally-java) via our other SDKs.

## Getting started

If you don't have an Apitally account yet, first [sign up here](https://app.apitally.io/?signup). Then create an app in the Apitally dashboard. You'll see detailed setup instructions with code snippets you can copy and paste. These also include your write token.

See the [SDK reference](https://docs.apitally.io/sdk-reference/python/v1/configuration) for all available configuration options, including how to mask sensitive data, capture request and response payloads, and more.

### FastAPI

Install the SDK with the `fastapi` extra, which also pulls in the OpenTelemetry instrumentation for FastAPI:

```bash
pip install "apitally[fastapi]"
```

Then initialize Apitally for your application:

```python
import apitally
from fastapi import FastAPI

app = FastAPI()
apitally.init(app, write_token="your-write-token", env="dev")
```

For further instructions, see our [setup guide for FastAPI](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/fastapi).

### Django

Install the SDK with the `django` extra, which also pulls in the OpenTelemetry instrumentation for Django:

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

Then initialize Apitally for your application:

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

Then initialize Apitally for your application:

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

Then add the Apitally plugin to your application:

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

Then initialize Apitally for your application:

```python
import apitally
from blacksheep import Application

app = Application()
apitally.init(app, write_token="your-write-token", env="dev")
```

For further instructions, see our [setup guide for BlackSheep](https://docs.apitally.io/sdk-reference/python/v1/setup-guides/blacksheep).

## Configuration

The write token and environment can also be provided via the `APITALLY_WRITE_TOKEN` and `APITALLY_ENV` environment variables instead of the `write_token` and `env` arguments. The environment defaults to `dev`.

By default, Apitally captures response headers but not request headers or request and response bodies. You can opt in with parameters:

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

Sensitive values in query parameters, headers, and body fields are masked automatically based on built-in patterns, and you can add your own via the `mask_query_params`, `mask_headers`, and `mask_body_fields` arguments.

On high-traffic applications you can capture logs and traces for only a fraction of requests by setting `sample_rate` (e.g. `0.1` for 10%), or decide per request with the `sample_on_request` and `sample_on_response` callbacks. Metrics always count every request, regardless of sampling.

Application logs written via the standard `logging` module are captured and correlated with requests by default. Use `mask_log_record` to transform or drop Apitally's captured copy, or opt out with `capture_logs=False`.

See the [SDK reference](https://docs.apitally.io/sdk-reference/python/v1/configuration) for all configuration options.

## Identifying consumers and more

The top-level `apitally` package provides functions you can call from anywhere in your request handling code:

```python
import apitally

# Associate the current request with an API consumer
apitally.set_consumer(user.identifier, name=user.name, group=user.group)

# Attach a custom attribute to the current request
apitally.set_request_attribute("tenant", tenant_id)

# Capture a handled exception for the current request
apitally.capture_exception(exc)
```

For further details, check out our [documentation](https://docs.apitally.io).

## Existing OpenTelemetry setup

If your app already uses an OpenTelemetry SDK tracer provider, configure it before initializing Apitally. Apitally automatically adds its span processor to your provider, keeping your existing exporters.

Your provider's sampling settings also affect Apitally. Requests dropped by the sampler will not have request logs or traces in Apitally. Metrics still include all requests, regardless of sampling.

## Trusted proxies

If your application runs behind a reverse proxy or load balancer, configure trusted proxies in your framework so Apitally can record the real client IP for GeoIP. Apitally uses the client IP reported by your framework. It does not read forwarding headers itself to determine the client IP.

## Getting help

If you need help please [create a new discussion](https://github.com/orgs/apitally/discussions/categories/q-a) on GitHub or email us at [support@apitally.io](mailto:support@apitally.io). We'll get back to you as soon as possible.

## License

This library is licensed under the terms of the [MIT license](LICENSE).
