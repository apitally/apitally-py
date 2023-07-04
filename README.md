# Starlette Apitally

[![Tests](https://github.com/apitally/starlette-apitally/actions/workflows/tests.yaml/badge.svg?event=push)](https://github.com/apitally/starlette-apitally/actions)
[![Codecov](https://codecov.io/gh/apitally/starlette-apitally/branch/main/graph/badge.svg?token=UNLYBY4Y3V)](https://codecov.io/gh/apitally/starlette-apitally)
[![PyPI](https://img.shields.io/pypi/v/starlette-apitally?logo=pypi&logoColor=white&color=%23006dad)](https://pypi.org/project/starlette-apitally/)

Apitally integration for Starlette / FastAPI.

The integration is implemented as a middleware which sends metrics about requests and responses to [Apitally](https://apitally.io) every 10 seconds. This is done asynchronously and does not impact the performance of your application.

## Installation

```bash
pip install starlette-apitally
```

## Usage with FastAPI

```python
from fastapi import FastAPI
from starlette_apitally import ApitallyMiddleware

app = FastAPI()
app.add_middleware(ApitallyMiddleware, client_id="<your-client-id>")
```

## Usage with Starlette

```python
from starlette.applications import Starlette
from starlette_apitally import ApitallyMiddleware

app = Starlette()
app.add_middleware(ApitallyMiddleware, client_id="<your-client-id>")
```
