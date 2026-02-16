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
<img alt="Apitally screenshots" src="https://assets.apitally.io/screenshots/overview.png">
<br>

# Apitally SDK for Python

[![Tests](https://github.com/apitally/apitally-py/actions/workflows/tests.yaml/badge.svg?event=push)](https://github.com/apitally/apitally-py/actions)
[![Codecov](https://codecov.io/gh/apitally/apitally-py/graph/badge.svg?token=UNLYBY4Y3V)](https://codecov.io/gh/apitally/apitally-py)
[![PyPI](https://img.shields.io/pypi/v/apitally?logo=pypi&logoColor=white&color=%23006dad)](https://pypi.org/project/apitally/)

Apitally is a simple API monitoring and analytics tool that makes it easy to understand how your APIs are used
and helps you troubleshoot API issues faster. Setup is easy and takes less than 5 minutes.

Learn more about Apitally on our 🌎 [website](https://apitally.io) or check out
the 📚 [documentation](https://docs.apitally.io).

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
| [**FastAPI**](https://github.com/fastapi/fastapi) \*                         | `>=0.94.1`         | [Link](https://docs.apitally.io/setup-guides/fastapi)               |
| [**Flask**](https://github.com/pallets/flask)                                | `>=2.0.0`          | [Link](https://docs.apitally.io/setup-guides/flask)                 |
| [**Django REST Framework**](https://github.com/encode/django-rest-framework) | `>=3.10.0`         | [Link](https://docs.apitally.io/setup-guides/django-rest-framework) |
| [**Django Ninja**](https://github.com/vitalik/django-ninja)                  | `>=1.0.0`          | [Link](https://docs.apitally.io/setup-guides/django-ninja)          |
| [**Starlette**](https://github.com/encode/starlette)                         | `>=0.26.1`         | [Link](https://docs.apitally.io/setup-guides/starlette)             |
| [**Litestar**](https://github.com/litestar-org/litestar)                     | `>=2.4.0`          | [Link](https://docs.apitally.io/setup-guides/litestar)              |
| [**BlackSheep**](https://github.com/Neoteroi/blacksheep)                     | `>=2.0.0`          | [Link](https://docs.apitally.io/setup-guides/blacksheep)            |

\* For FastAPI on Cloudflare Workers use our [Python Serverless SDK](https://github.com/apitally/apitally-py-serverless) instead.

Apitally also supports many other web frameworks in [JavaScript](https://github.com/apitally/apitally-js), [Go](https://github.com/apitally/apitally-go), [.NET](https://github.com/apitally/apitally-dotnet) and [Java](https://github.com/apitally/apitally-java) via our other SDKs.

## Getting started

If you don't have an Apitally account yet, first [sign up here](https://app.apitally.io/?signup). Then create an app in the Apitally dashboard. You'll see detailed setup instructions with code snippets you can copy and paste. These also include your client ID.

See the [SDK reference](https://docs.apitally.io/sdk-reference/python) for all available configuration options, including how to mask sensitive data, customize request logging, and more.

### FastAPI

Install the SDK with the `fastapi` extra:

```bash
pip install "apitally[fastapi]"
```

Then add the Apitally middleware to your application:

```python
from fastapi import FastAPI
from apitally.fastapi import ApitallyMiddleware

app = FastAPI()
app.add_middleware(
    ApitallyMiddleware,
    client_id="your-client-id",
    env="dev",  # or "prod" etc.
)
```

For further instructions, see our
[setup guide for FastAPI](https://docs.apitally.io/setup-guides/fastapi).

### Django

Install the SDK with the `django_rest_framework` or `django_ninja` extra:

```bash
pip install "apitally[django_rest_framework]"
# or
pip install "apitally[django_ninja]"
```

Then add the Apitally middleware to your Django settings:

```python
MIDDLEWARE = [
    "apitally.django.ApitallyMiddleware",
    # Other middleware ...
]
APITALLY_MIDDLEWARE = {
    "client_id": "your-client-id",
    "env": "dev",  # or "prod" etc.
}
```

For further instructions, see our
[setup guide for Django](https://docs.apitally.io/setup-guides/django).

### Flask

Install the SDK with the `flask` extra:

```bash
pip install "apitally[flask]"
```

Then add the Apitally middleware to your application:

```python
from flask import Flask
from apitally.flask import ApitallyMiddleware

app = Flask(__name__)
app.wsgi_app = ApitallyMiddleware(
    app,
    client_id="your-client-id",
    env="dev",  # or "prod" etc.
)
```

For further instructions, see our
[setup guide for Flask](https://docs.apitally.io/setup-guides/flask).

### Starlette

Install the SDK with the `starlette` extra:

```bash
pip install "apitally[starlette]"
```

Then add the Apitally middleware to your application:

```python
from starlette.applications import Starlette
from apitally.starlette import ApitallyMiddleware

app = Starlette(routes=[...])
app.add_middleware(
    ApitallyMiddleware,
    client_id="your-client-id",
    env="dev",  # or "prod" etc.
)
```

For further instructions, see our
[setup guide for Starlette](https://docs.apitally.io/setup-guides/starlette).

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
    plugins=[
        ApitallyPlugin(
            client_id="your-client-id",
            env="dev",  # or "prod" etc.
        ),
    ]
)
```

For further instructions, see our
[setup guide for Litestar](https://docs.apitally.io/setup-guides/litestar).

### BlackSheep

Install the SDK with the `blacksheep` extra:

```bash
pip install "apitally[blacksheep]"
```

Then add the Apitally middleware to your application:

```python
from blacksheep import Application
from apitally.blacksheep import use_apitally

app = Application()
use_apitally(
    app,
    client_id="your-client-id",
    env="dev",  # or "prod" etc.
)
```

For further instructions, see our
[setup guide for BlackSheep](https://docs.apitally.io/setup-guides/blacksheep).

## Getting help

If you need help please
[create a new discussion](https://github.com/orgs/apitally/discussions/categories/q-a)
on GitHub or email us at [support@apitally.io](mailto:support@apitally.io). We'll get back to you as soon as possible.

## License

This library is licensed under the terms of the [MIT license](LICENSE).
