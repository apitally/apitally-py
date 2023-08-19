from django.http import HttpRequest, HttpResponse
from django.urls import path
from ninja import NinjaAPI

from apitally.django_ninja import (
    AuthorizationAPIKeyHeader,
    InvalidAPIKey,
    PermissionDenied,
)


api = NinjaAPI()


@api.exception_handler(InvalidAPIKey)
def on_invalid_api_key(request: HttpRequest, exc) -> HttpResponse:
    return api.create_response(request, {"detail": "Invalid API key"}, status=403)


@api.exception_handler(PermissionDenied)
def on_permission_denied(request: HttpRequest, exc) -> HttpResponse:
    return api.create_response(request, {"detail": "Permission denied"}, status=403)


@api.get("/foo", auth=AuthorizationAPIKeyHeader())
def foo(request: HttpRequest) -> str:
    return "foo"


@api.get("/foo/{bar}", auth=AuthorizationAPIKeyHeader(scopes=["foo"]))
def foo_bar(request: HttpRequest, bar: int) -> str:
    return f"foo: {bar}"


@api.post("/bar", auth=AuthorizationAPIKeyHeader(scopes=["bar"]))
def bar(request: HttpRequest) -> str:
    return "bar"


@api.put("/baz", auth=AuthorizationAPIKeyHeader())
def baz(request: HttpRequest) -> str:
    raise ValueError("baz")


urlpatterns = [
    path("api/", api.urls),  # type: ignore[arg-type]
]
