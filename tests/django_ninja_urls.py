from django.http import HttpRequest
from django.urls import path
from ninja import NinjaAPI

from apitally.django_ninja import APIKeyAuth


api = NinjaAPI()


@api.get("/foo", auth=APIKeyAuth())
def foo(request: HttpRequest) -> str:
    return "foo"


@api.get("/foo/{bar}", auth=APIKeyAuth(scopes=["foo"]))
def foo_bar(request: HttpRequest, bar: int) -> str:
    return f"foo: {bar}"


@api.post("/bar", auth=APIKeyAuth(scopes=["bar"]))
def bar(request: HttpRequest) -> str:
    return "bar"


@api.put("/baz", auth=APIKeyAuth())
def baz(request: HttpRequest) -> str:
    raise ValueError("baz")


urlpatterns = [
    path("api/", api.urls),  # type: ignore[arg-type]
]
