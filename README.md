<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/apitally/assets/main/logo-vertical-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/apitally/assets/main/logo-vertical-light.png">
    <img alt="Apitally logo" src="https://raw.githubusercontent.com/apitally/assets/main/logo-vertical-light.png">
  </picture>
</p>

<p align="center"><b>Your refreshingly simple REST API companion.</b></p>

<p align="center"><i>Apitally offers API traffic monitoring and integrated API key management that is extremely easy to set up and use with new and existing API projects. No assumptions made about your infrastructure, no extra tools for you to host and maintain.</i></p>

<p align="center">🔗 <b><a href="https://apitally.io" target="_blank">apitally.io</a></b></p>

![Apitally screenshots](https://raw.githubusercontent.com/apitally/assets/main/overview.png)

---

# Apitally client for Python

[![Tests](https://github.com/apitally/python-client/actions/workflows/tests.yaml/badge.svg?event=push)](https://github.com/apitally/python-client/actions)
[![Codecov](https://codecov.io/gh/apitally/python-client/graph/badge.svg?token=UNLYBY4Y3V)](https://codecov.io/gh/apitally/python-client)
[![PyPI](https://img.shields.io/pypi/v/apitally?logo=pypi&logoColor=white&color=%23006dad)](https://pypi.org/project/apitally/)

This client library currently supports the following frameworks:

- [FastAPI](https://docs.apitally.io/frameworks/fastapi)
- [Starlette](https://docs.apitally.io/frameworks/starlette)
- [Flask](https://docs.apitally.io/frameworks/flask)
- [Django Ninja](https://docs.apitally.io/frameworks/django-ninja)
- [Django REST Framework](https://docs.apitally.io/frameworks/django-rest-framework)

## Install

Use `pip` to install and provide your framework of choice as an extra, for example:

```bash
pip install apitally[fastapi]
```

The available extras are: `fastapi`, `starlette`, `flask`, `django_ninja` and `django_rest_framework`.

## Usage

Below are basic usage examples for each supported framework. For further instructions and examples, including how to identify consumers and use API key authentication, check out our [documentation](https://docs.apitally.io/).

### FastAPI

```python
from fastapi import FastAPI
from apitally.fastapi import ApitallyMiddleware

app = FastAPI()
app.add_middleware(
    ApitallyMiddleware,
    client_id="your-client-id",
    env="your-env-name",
)
```

### Starlette

```python
from starlette.applications import Starlette
from apitally.starlette import ApitallyMiddleware

app = Starlette(routes=[...])
app.add_middleware(
    ApitallyMiddleware,
    client_id="your-client-id",
    env="your-env-name",
)
```

### Flask

```python
from flask import Flask
from apitally.flask import ApitallyMiddleware

app = Flask(__name__)
app.wsgi_app = ApitallyMiddleware(
    app,
    client_id="your-client-id",
    env="your-env-name",
)
```

### Django Ninja

In your Django `settings.py` file:

```python
MIDDLEWARE = [
    # Other middlewares first ...
    "apitally.django_ninja.ApitallyMiddleware",
]
APITALLY_MIDDLEWARE = {
    "client_id": "your-client-id",
    "env": "your-env-name",
}
```

### Django REST Framework

In your Django `settings.py` file:

```python
MIDDLEWARE = [
    # Other middlewares first ...
    "apitally.django_rest_framework.ApitallyMiddleware",
]
APITALLY_MIDDLEWARE = {
    "client_id": "your-client-id",
    "env": "your-env-name",
}
```

## Getting help

If you need help please join our [Apitally community on Slack](https://apitally-community.slack.com/) or [create a new discussion](https://github.com/orgs/apitally/discussions/categories/q-a) on GitHub.

## License

This library is licensed under the terms of the MIT license.
