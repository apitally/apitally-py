# Apitally client for Python

[![Tests](https://github.com/apitally/apitally-python/actions/workflows/tests.yaml/badge.svg?event=push)](https://github.com/apitally/apitally-python/actions)
[![Codecov](https://codecov.io/gh/apitally/apitally-python/branch/main/graph/badge.svg?token=UNLYBY4Y3V)](https://codecov.io/gh/apitally/apitally-python)
[![PyPI](https://img.shields.io/pypi/v/apitally?logo=pypi&logoColor=white&color=%23006dad)](https://pypi.org/project/apitally/)

Apitally client library for Python.

## Installation

```bash
pip install apitally
```

## Usage with FastAPI

```python
from fastapi import FastAPI
from apitally.fastapi import ApitallyMiddleware

app = FastAPI()
app.add_middleware(ApitallyMiddleware, client_id="<your-client-id>")
```

## Usage with Starlette

```python
from starlette.applications import Starlette
from apitally.starlette import ApitallyMiddleware

app = Starlette()
app.add_middleware(ApitallyMiddleware, client_id="<your-client-id>")
```
