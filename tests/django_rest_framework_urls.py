from django.urls import path
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apitally.django_rest_framework import HasAPIKey


class FooView(APIView):
    permission_classes = [HasAPIKey]

    def get(self, request: Request) -> Response:
        return Response("foo")


class FooBarView(APIView):
    permission_classes = [HasAPIKey]
    required_scopes = ["foo"]

    def get(self, request: Request, bar: int) -> Response:
        return Response(f"foo: {bar}")


class BarView(APIView):
    permission_classes = [HasAPIKey]
    required_scopes = ["bar"]

    def post(self, request: Request) -> Response:
        return Response("bar")


class BazView(APIView):
    permission_classes = [HasAPIKey]

    def put(self, request: Request) -> Response:
        raise ValueError("baz")


urlpatterns = [
    path("foo/", FooView.as_view()),
    path("foo/<int:bar>/", FooBarView.as_view()),
    path("bar/", BarView.as_view()),
    path("baz/", BazView.as_view()),
]
