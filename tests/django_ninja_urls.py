from typing import Dict

from django.http import HttpRequest
from django.urls import path
from ninja import NinjaAPI, Schema


api = NinjaAPI()


@api.get("/foo", summary="Foo", description="Foo")
def foo(request: HttpRequest) -> str:
    return "foo"


@api.get("/foo/{bar}")
def foo_bar(request: HttpRequest, bar: int) -> Dict[str, int]:
    return {"foo": bar}


class BarRequestBody(Schema):
    foo: str


@api.post("/bar")
def bar(request: HttpRequest, item: BarRequestBody) -> Dict[str, str]:
    return {"bar": item.foo}


@api.put("/baz")
def baz(request: HttpRequest) -> str:
    request.apitally_consumer = "baz"  # type: ignore[attr-defined]
    raise ValueError("baz")


@api.get("/val")
def val(request: HttpRequest, foo: int) -> str:
    return "val"


urlpatterns = [
    path("api/", api.urls),  # type: ignore[arg-type]
]
