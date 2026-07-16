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
<p align="center" style="color: #ccc;">Metrics, logs, traces, and alerts for your APIs, with just one line of code.</p>
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
Get started in minutes by just adding a line of code. No infrastructure changes required, no dashboards to build.

The SDK is an [OpenTelemetry](https://opentelemetry.io) distribution: it builds on the community OTel instrumentations for each framework and works alongside an existing OpenTelemetry setup if you have one.

Learn more about Apitally on our 🌎 [website](https://apitally.io) or check out
the 📚 [documentation](https://docs.apitally.io).

> [!IMPORTANT]
> **Upgrading from 0.x?** Version 1.0 is a rewrite with a new setup API. See the [migration guide](MIGRATION.md) for a full 0.x to 1.x mapping, including one behavior change you should know about before upgrading.

## Key features

### API analytics

Track traffic, error and performance metrics for your API, each endpoint and
individual API consumers, allowing you to make informed, data-driven engineering
and product decisions.

### Request logs

Drill down from insights to individual API requests or use powerful search and filters to
find specific requests. View correlated application logs and traces for a complete picture
of each request, making troubleshooting faster and easier.

### Error tracking

Understand which validation rules in your endpoints cause client errors. Capture
error details and stack traces for 500 error responses, and have them linked to
Sentry issues automatically.

### API monitoring & alerts

Get notified immediately if something isn't right using custom alerts, synthetic
uptime checks and heartbeat monitoring. Alert notifications can be delivered via
email, Slack and Microsoft Teams.

## Supported frameworks

| Framework                                                                    | Supported versions | Setup guide                                                         |
| ---------------------------------------------------------------------------- | ------------------ | ------------------------------------------------------------------- |
| [**FastAPI**](https://github.com/fastapi/fastapi) \*                         | `>=0.108.0`        | [Link](https://docs.apitally.io/setup-guides/fastapi)               |
| [**Flask**](https://github.com/pallets/flask)                                | `>=2.0.0`          | [Link](https://docs.apitally.io/setup-guides/flask)                 |
| [**Django**](https://github.com/django/django)                               | `>=3.2`            | [Link](https://docs.apitally.io/setup-guides/django)                |
| [**Django REST Framework**](https://github.com/encode/django-rest-framework) | `>=3.12.0`         | [Link](https://docs.apitally.io/setup-guides/django-rest-framework) |
| [**Django Ninja**](https://github.com/vitalik/django-ninja)                  | `>=1.0.0`          | [Link](https://docs.apitally.io/setup-guides/django-ninja)          |
| [**Starlette**](https://github.com/encode/starlette)                         | `>=0.29.0`         | [Link](https://docs.apitally.io/setup-guides/starlette)             |
| [**Litestar**](https://github.com/litestar-org/litestar)                     | `>=2.24.0`         | [Link](https://docs.apitally.io/setup-guides/litestar)              |
| [**BlackSheep**](https://github.com/Neoteroi/blacksheep)                     | `>=2.6.1`          | [Link](https://docs.apitally.io/setup-guides/blacksheep)            |

\* For FastAPI on Cloudflare Workers use our [Python Serverless SDK](https://github.com/apitally/apitally-py-serverless) instead.

Apitally also supports many other web frameworks in [JavaScript](https://github.com/apitally/apitally-js), [Go](https://github.com/apitally/apitally-go), [.NET](https://github.com/apitally/apitally-dotnet) and [Java](https://github.com/apitally/apitally-java) via our other SDKs.

## Getting started

If you don't have an Apitally account yet, first [sign up here](https://app.apitally.io/?signup). Then create an app in the Apitally dashboard. You'll see detailed setup instructions with code snippets you can copy and paste. These also include your write token.

Setup is a single call to `apitally.init`, which detects your framework from the app instance:

```python
import apitally

apitally.init(app, write_token="your-write-token")
```

Django apps call `apitally.init()` without an app argument at the end of `settings.py`, and Litestar apps use `ApitallyPlugin` instead. See the framework sections below for details.

See the [SDK reference](https://docs.apitally.io/sdk-reference/python) for all available configuration options, including how to mask sensitive data, capture request and response payloads, and more.

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
apitally.init(app, write_token="your-write-token")
```

For further instructions, see our
[setup guide for FastAPI](https://docs.apitally.io/setup-guides/fastapi).

### Django

Install the SDK with the `django` extra, which also pulls in the OpenTelemetry instrumentation for Django. The same extra covers plain Django, Django REST Framework and Django Ninja:

```bash
pip install "apitally[django]"
```

Then call `apitally.init()` at the *end* of your `settings.py` module. The placement matters: it must run after `MIDDLEWARE` is defined, as Apitally inserts its own middleware automatically.

```python
# settings.py

MIDDLEWARE = [
    # Your middleware ...
]

# ... at the very end of the file:
import apitally

apitally.init(write_token="your-write-token")
```

For further instructions, see our
[setup guide for Django](https://docs.apitally.io/setup-guides/django).

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
apitally.init(app, write_token="your-write-token")
```

For further instructions, see our
[setup guide for Flask](https://docs.apitally.io/setup-guides/flask).

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
apitally.init(app, write_token="your-write-token")
```

For further instructions, see our
[setup guide for Starlette](https://docs.apitally.io/setup-guides/starlette).

### Litestar

Install the SDK with the `litestar` extra:

```bash
pip install "apitally[litestar]"
```

Litestar plugins must be passed at construction, so setup uses `ApitallyPlugin` instead of `apitally.init`:

```python
from litestar import Litestar
from apitally.litestar import ApitallyPlugin

app = Litestar(
    route_handlers=[...],
    plugins=[ApitallyPlugin(write_token="your-write-token")],
)
```

For further instructions, see our
[setup guide for Litestar](https://docs.apitally.io/setup-guides/litestar).

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
apitally.init(app, write_token="your-write-token")
```

For further instructions, see our
[setup guide for BlackSheep](https://docs.apitally.io/setup-guides/blacksheep).

## Configuration

The write token and environment can also be provided via the `APITALLY_WRITE_TOKEN` and `APITALLY_ENV` environment variables instead of the `write_token` and `env` arguments. The environment defaults to `prod`.

Out of the box, Apitally captures metrics, request logs, traces, exceptions, application logs, and response headers. Request headers and request and response bodies are *not* captured by default. You can opt in per direction:

```python
apitally.init(
    app,
    write_token="your-write-token",
    log_request_headers=True,
    log_request_body=True,
    log_response_body=True,
)
```

Sensitive values in query parameters, headers, and body fields are masked automatically based on built-in patterns, and you can add your own via the `mask_query_params`, `mask_headers`, and `mask_body_fields` arguments.

On high-traffic applications you can capture traces and logs for only a fraction of requests by setting `sample_rate` (e.g. `0.1` for 10%), or decide per request with the `sample_on_request` and `sample_on_response` callbacks. A request's telemetry is held in memory until the request completes (up to 1,000 spans and 1,000 log records per request), so a request dropped by `sample_on_response` never counts toward your quota. Request-stage sampling additionally skips capture work such as body buffering and masking (for Flask, request bodies are still buffered because the sampling decision happens later). Metrics always count every request, regardless of sampling.

Application logs written via the standard `logging` module are captured and correlated with requests by default. Log messages are exported as-is, so if your application logs sensitive data, sanitize it at the source or opt out with `capture_logs=False`.

See the [SDK reference](https://docs.apitally.io/sdk-reference/python) for all configuration options.

## Identifying consumers and more

The top-level `apitally` package provides functions you can call from anywhere in your request handling code, for example from your authentication middleware or dependencies:

```python
from apitally import capture_exception, set_consumer, set_request_attribute

# Associate the current request with an API consumer
set_consumer(user.identifier, name=user.name, group=user.group)

# Attach a custom attribute to the current request
set_request_attribute("tenant", tenant_id)

# Capture a handled exception for the current request
capture_exception(exc)
```

For further details, check out our [documentation](https://docs.apitally.io).

## Getting help

If you need help please
[create a new discussion](https://github.com/orgs/apitally/discussions/categories/q-a)
on GitHub or email us at [support@apitally.io](mailto:support@apitally.io). We'll get back to you as soon as possible.

## License

This library is licensed under the terms of the [MIT license](LICENSE).
