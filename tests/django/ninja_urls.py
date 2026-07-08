from django.http import HttpRequest
from django.urls import path
from ninja import NinjaAPI


api = NinjaAPI()


@api.get("/foo", summary="Foo")
def foo(request: HttpRequest) -> str:
    return "foo"


@api.get("/foo/{bar}")
def foo_bar(request: HttpRequest, bar: int) -> dict[str, int]:
    return {"foo": bar}


urlpatterns = [
    path("api/", api.urls),
]
