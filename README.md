# Apitally client for Python

[![Tests](https://github.com/apitally/apitally-python/actions/workflows/tests.yaml/badge.svg?event=push)](https://github.com/apitally/apitally-python/actions)
[![Codecov](https://codecov.io/gh/apitally/apitally-python/branch/main/graph/badge.svg?token=UNLYBY4Y3V)](https://codecov.io/gh/apitally/apitally-python)
[![PyPI](https://img.shields.io/pypi/v/apitally?logo=pypi&logoColor=white&color=%23006dad)](https://pypi.org/project/apitally/)

Apitally client library for Python.

Currently supports the following frameworks:

- [FastAPI](https://fastapi.tiangolo.com/)
- [Starlette](https://www.starlette.io/)
- [Django Ninja](https://django-ninja.rest-framework.com/)
- [Django REST Framework](https://www.django-rest-framework.org/)
- [Flask](https://flask.palletsprojects.com/)

## Installation

Use `pip` to install and provide your framework of choice as an extra, for example:

```bash
pip install apitally[fastapi]
```

The available extras are: `fastapi`, `starlette`, `django_ninja`, `django_rest_framework` and `flask`.

## Basic usage

Below are basic usage examples for each supported framework. For more detailed instructions and examples, including on how to use Apitally API key authentication, see the [documentation](https://docs.apitally.com/).

### With FastAPI

```python
from fastapi import FastAPI
from apitally.fastapi import ApitallyMiddleware

app = FastAPI()
app.add_middleware(ApitallyMiddleware, client_id="<your-client-id>")
```

### With Starlette

```python
from starlette.applications import Starlette
from apitally.starlette import ApitallyMiddleware

app = Starlette()
app.add_middleware(ApitallyMiddleware, client_id="<your-client-id>")
```

### With Django Ninja

In your Django `settings.py` file:

```python
MIDDLEWARE = [
    "apitally.django_ninja.ApitallyMiddleware",
]
APITALLY_MIDDLEWARE = {
    "client_id": "<your-client-id>",
}
```

### With Django REST Framework

In your Django `settings.py` file:

```python
MIDDLEWARE = [
    "apitally.django_rest_framework.ApitallyMiddleware",
]
APITALLY_MIDDLEWARE = {
    "client_id": "<your-client-id>",
}
```

### With Flask

```python
from flask import Flask
from apitally.flask import ApitallyMiddleware

app = Flask(__name__)
app.wsgi_app = ApitallyMiddleware(app.wsgi_app, client_id="<your-client-id>")
```
