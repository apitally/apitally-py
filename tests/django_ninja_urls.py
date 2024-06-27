from django.http import HttpRequest
from django.urls import path
from ninja import NinjaAPI


api = NinjaAPI()


@api.get("/foo", summary="Foo", description="Foo")
def foo(request: HttpRequest) -> str:
    return "foo"


@api.get("/foo/{bar}")
def foo_bar(request: HttpRequest, bar: int) -> str:
    return f"foo: {bar}"


@api.post("/bar")
def bar(request: HttpRequest) -> str:
    return "bar"


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
