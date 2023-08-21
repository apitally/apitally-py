from django.http import HttpRequest, HttpResponse
from django.urls import path
from rest_framework.views import APIView

from apitally.django_rest_framework import HasAPIKey


class FooView(APIView):
    permission_classes = [HasAPIKey]

    def get(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse("foo")


class FooBarView(APIView):
    permission_classes = [HasAPIKey]
    required_scopes = ["foo"]

    def get(self, request: HttpRequest, bar: int) -> HttpResponse:
        return HttpResponse(f"foo: {bar}")


class BarView(APIView):
    permission_classes = [HasAPIKey]
    required_scopes = ["bar"]

    def post(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse("bar")


class BazView(APIView):
    permission_classes = [HasAPIKey]

    def put(self, request: HttpRequest) -> HttpResponse:
        raise ValueError("baz")


urlpatterns = [
    path("foo/", FooView.as_view()),
    path("foo/<int:bar>/", FooBarView.as_view()),
    path("bar/", BarView.as_view()),
    path("baz/", BazView.as_view()),
]
