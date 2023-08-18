from django.http import HttpRequest, HttpResponse
from django.urls import path
from rest_framework.views import APIView


class FooBarView(APIView):
    def get(self, request: HttpRequest, bar: int) -> HttpResponse:
        return HttpResponse(f"foo: {bar}")


class BarView(APIView):
    def post(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse("bar")


class BazView(APIView):
    def put(self, request: HttpRequest) -> HttpResponse:
        raise ValueError("baz")


urlpatterns = [
    path("foo/<int:bar>/", FooBarView.as_view()),
    path("bar/", BarView.as_view()),
    path("baz/", BazView.as_view()),
]
