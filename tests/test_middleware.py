import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from starlette_apitally.middleware import ApitallyMiddleware


@pytest.fixture(scope="module")
def app(self):
    app_ = Starlette()
    app_.add_middleware(ApitallyMiddleware)

    @app_.route("/foo/")
    def foo(request: Request):
        return PlainTextResponse("Foo")

    @app_.route("/bar/")
    def bar(request: Request):
        raise ValueError("bar")

    @app_.route("/foo/{bar}/")
    def foobar(request: Request):
        return PlainTextResponse(f"Foo: {request.path_params['bar']}")

    return app_
