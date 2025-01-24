<p align="center">
  <a href="https://apitally.io" target="_blank">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://assets.apitally.io/logos/logo-vertical-dark.png">
      <source media="(prefers-color-scheme: light)" srcset="https://assets.apitally.io/logos/logo-vertical-light.png">
      <img alt="Apitally logo" src="https://assets.apitally.io/logos/logo-vertical-light.png" width="150">
    </picture>
  </a>
</p>

<p align="center"><b>Simple, privacy-focused API monitoring & analytics</b></p>

<p align="center"><i>Apitally helps you understand how your APIs are being used and alerts you when things go wrong.<br>Just add two lines of code to your project to get started.</i></p>
<br>

![Apitally screenshots](https://assets.apitally.io/screenshots/overview.png)

---

# Apitally client library for Python

[![Tests](https://github.com/apitally/apitally-py/actions/workflows/tests.yaml/badge.svg?event=push)](https://github.com/apitally/apitally-py/actions)
[![Codecov](https://codecov.io/gh/apitally/apitally-py/graph/badge.svg?token=UNLYBY4Y3V)](https://codecov.io/gh/apitally/apitally-py)
[![PyPI](https://img.shields.io/pypi/v/apitally?logo=pypi&logoColor=white&color=%23006dad)](https://pypi.org/project/apitally/)

This client library for Apitally currently supports the following Python web
frameworks:

- [FastAPI](https://docs.apitally.io/frameworks/fastapi)
- [Django REST Framework](https://docs.apitally.io/frameworks/django-rest-framework)
- [Django Ninja](https://docs.apitally.io/frameworks/django-ninja)
- [Flask](https://docs.apitally.io/frameworks/flask)
- [Starlette](https://docs.apitally.io/frameworks/starlette)
- [Litestar](https://docs.apitally.io/frameworks/litestar)

Learn more about Apitally on our 🌎 [website](https://apitally.io) or check out
the 📚 [documentation](https://docs.apitally.io).

## Key features

### API analytics

Track traffic, error and performance metrics for your API, each endpoint and individual API consumers, allowing you to make informed, data-driven engineering and product decisions.

### Error tracking

Understand which validation rules in your endpoints cause client errors. Capture error details and stack traces for 500 error responses, and have them linked to Sentry issues automatically.

### Request logging

Drill down from insights to individual requests or use powerful filtering to understand how consumers have interacted with your API. Configure exactly what is included in the logs to meet your requirements.

### API monitoring & alerting

Get notified immediately if something isn't right using custom alerts, synthetic uptime checks and heartbeat monitoring. Notifications can be delivered via email, Slack or Microsoft Teams.

## Install

Use `pip` to install and provide your framework of choice as an extra, for
example:

```bash
pip install apitally[fastapi]
```

The available extras are: `fastapi`, `flask`, `django_rest_framework`,
`django_ninja`, `starlette` and `litestar`.

## Usage

Our [setup guides](https://docs.apitally.io/quickstart) include all the details
you need to get started.

### FastAPI

This is an example of how to add the Apitally middleware to a FastAPI
application. For further instructions, see our
[setup guide for FastAPI](https://docs.apitally.io/frameworks/fastapi).

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

### Django

This is an example of how to add the Apitally middleware to a Django Ninja or
Django REST Framework application. For further instructions, see our
[setup guide for Django](https://docs.apitally.io/frameworks/django).

In your Django `settings.py` file:

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

### Flask

This is an example of how to add the Apitally middleware to a Flask application.
For further instructions, see our
[setup guide for Flask](https://docs.apitally.io/frameworks/flask).

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

### Starlette

This is an example of how to add the Apitally middleware to a Starlette application.
For further instructions, see our
[setup guide for Starlette](https://docs.apitally.io/frameworks/starlette).

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

### Litestar

This is an example of how to add the Apitally plugin to a Litestar application.
For further instructions, see our
[setup guide for Litestar](https://docs.apitally.io/frameworks/litestar).

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

## Getting help

If you need help please
[create a new discussion](https://github.com/orgs/apitally/discussions/categories/q-a)
on GitHub or
[join our Slack workspace](https://join.slack.com/t/apitally-community/shared_invite/zt-2b3xxqhdu-9RMq2HyZbR79wtzNLoGHrg).

## License

This library is licensed under the terms of the MIT license.
