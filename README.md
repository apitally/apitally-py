<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://assets.apitally.io/logos/logo-vertical-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="https://assets.apitally.io/logos/logo-vertical-light.png">
    <img alt="Apitally logo" src="https://assets.apitally.io/logos/logo-vertical-light.png">
  </picture>
</p>

<p align="center"><b>Your refreshingly simple REST API companion.</b></p>

<p align="center"><i>Apitally is a simple and affordable API monitoring and API key management solution with a focus on data privacy. It is easy to set up and use for new and existing API projects using Python or Node.js.</i></p>

<p align="center">🔗 <b><a href="https://apitally.io" target="_blank">apitally.io</a></b></p>

![Apitally screenshots](https://assets.apitally.io/screenshots/overview.png)

---

# Apitally client library for Python

[![Tests](https://github.com/apitally/python-client/actions/workflows/tests.yaml/badge.svg?event=push)](https://github.com/apitally/python-client/actions)
[![Codecov](https://codecov.io/gh/apitally/python-client/graph/badge.svg?token=UNLYBY4Y3V)](https://codecov.io/gh/apitally/python-client)
[![PyPI](https://img.shields.io/pypi/v/apitally?logo=pypi&logoColor=white&color=%23006dad)](https://pypi.org/project/apitally/)

This client library for Apitally currently supports the following Python web
frameworks:

- [FastAPI](https://docs.apitally.io/frameworks/fastapi)
- [Starlette](https://docs.apitally.io/frameworks/starlette)
- [Flask](https://docs.apitally.io/frameworks/flask)
- [Django Ninja](https://docs.apitally.io/frameworks/django-ninja)
- [Django REST Framework](https://docs.apitally.io/frameworks/django-rest-framework)

Learn more about Apitally on our 🌎 [website](https://apitally.io) or check out
the 📚 [documentation](https://docs.apitally.io).

## Key features

- Middleware for different frameworks to capture metadata about API endpoints,
  requests and responses (no sensitive data is captured)
- Non-blocking clients that aggregate and send captured data to Apitally and
  optionally synchronize API key hashes in 1 minute intervals
- Functions to easily secure endpoints with API key authentication and
  permission checks

## Install

Use `pip` to install and provide your framework of choice as an extra, for
example:

```bash
pip install apitally[fastapi]
```

The available extras are: `fastapi`, `starlette`, `flask`, `django_ninja` and
`django_rest_framework`.

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
    env="default",  # or "dev", "prod" etc.
)
```

### Starlette

This is an example of how to add the Apitally middleware to a Starlette
application. For further instructions, see our
[setup guide for Starlette](https://docs.apitally.io/frameworks/starlette).

```python
from starlette.applications import Starlette
from apitally.starlette import ApitallyMiddleware

app = Starlette(routes=[...])
app.add_middleware(
    ApitallyMiddleware,
    client_id="your-client-id",
    env="default",  # or "dev", "prod" etc.
)
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
    env="default",  # or "dev", "prod" etc.
)
```

### Django Ninja

This is an example of how to add the Apitally middleware to a Django Ninja
application. For further instructions, see our
[setup guide for Django Ninja](https://docs.apitally.io/frameworks/django-ninja).

In your Django `settings.py` file:

```python
MIDDLEWARE = [
    "apitally.django_ninja.ApitallyMiddleware",
    # Other middleware ...
]
APITALLY_MIDDLEWARE = {
    "client_id": "your-client-id",
    "env": "default",  # or "dev", "prod" etc.
}
```

### Django REST Framework

This is an example of how to add the Apitally middleware to a Django REST
Framework application. For further instructions, see our
[setup guide for Django REST Framework](https://docs.apitally.io/frameworks/django-rest-framework).

In your Django `settings.py` file:

```python
MIDDLEWARE = [
    "apitally.django_rest_framework.ApitallyMiddleware",
    # Other middleware ...
]
APITALLY_MIDDLEWARE = {
    "client_id": "your-client-id",
    "env": "default",  # or "dev", "prod" etc.
}
```

## Getting help

If you need help please
[create a new discussion](https://github.com/orgs/apitally/discussions/categories/q-a)
on GitHub.

## License

This library is licensed under the terms of the MIT license.
