from django.http import HttpRequest, HttpResponse
from django.urls import path
from django.views import View
from django.views.decorators.http import require_http_methods
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView


class FooView(APIView):
    """Foo"""

    def get(self, request: Request) -> Response:
        return Response("foo")


class FooBarView(APIView):
    def get(self, request: Request, bar: int) -> Response:
        request._request.apitally_consumer = "test"  # type: ignore[attr-defined]
        return Response({"foo": bar})


@api_view(["POST"])
def bar(request: Request) -> Response:
    return Response({"bar": request.data["foo"]})


@api_view(["PUT"])
def baz(request: Request) -> Response:
    raise ValueError("baz")


class SimpleClassView(View):
    def get(self, request: HttpRequest, pk: int, *args, **kwargs) -> HttpResponse:
        return HttpResponse(f"Hello from a class-based Django view with pk={pk}")


@require_http_methods(["GET"])
def simple_view(request: HttpRequest, pk: int) -> HttpResponse:
    return HttpResponse("Hello from a regular Django view")


urlpatterns = [
    path("foo/", FooView.as_view()),
    path("foo/<int:bar>/", FooBarView.as_view()),
    path("bar/", bar),
    path("baz/", baz),
    path("class/<int:pk>/", SimpleClassView.as_view()),
    path("func/<int:pk>/", simple_view),
]
