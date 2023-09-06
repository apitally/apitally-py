from django.http import HttpRequest
from django.urls import path
from ninja import NinjaAPI

from apitally.django_ninja import APIKeyAuth, APIKeyAuthBase


class CustomAPIKeyAuth(APIKeyAuthBase):
    param_name = "ApiKey"


api = NinjaAPI()


@api.get("/foo", auth=CustomAPIKeyAuth())
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
    request.consumer_identifier = "baz"  # type: ignore[attr-defined]
    raise ValueError("baz")


@api.get("/val")
def val(request: HttpRequest, foo: int) -> str:
    return "val"


urlpatterns = [
    path("api/", api.urls),  # type: ignore[arg-type]
]
