from django.urls import path
from rest_framework.decorators import api_view, permission_classes
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apitally.django_rest_framework import HasAPIKey, HasAPIKeyWithScopes


class FooView(APIView):
    permission_classes = [HasAPIKey]

    def get(self, request: Request) -> Response:
        return Response("foo")


class FooBarView(APIView):
    permission_classes = [HasAPIKeyWithScopes(["foo"])]

    def get(self, request: Request, bar: int) -> Response:
        return Response(f"foo: {bar}")


@api_view(["POST"])
@permission_classes([HasAPIKeyWithScopes(["bar"])])
def bar(request: Request) -> Response:
    return Response("bar")


@api_view(["PUT"])
@permission_classes([HasAPIKey])
def baz(request: Request) -> Response:
    raise ValueError("baz")


urlpatterns = [
    path("foo/", FooView.as_view()),
    path("foo/<int:bar>/", FooBarView.as_view()),
    path("bar/", bar),
    path("baz/", baz),
]
