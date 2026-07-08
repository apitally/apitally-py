import json

from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.urls import include, path
from django.views import View

from apitally import set_consumer


def get_item(request: HttpRequest, pk: int) -> JsonResponse:
    return JsonResponse({"id": pk})


def create_item(request: HttpRequest) -> JsonResponse:
    return JsonResponse(json.loads(request.body), status=201)


def stream(request: HttpRequest) -> StreamingHttpResponse:
    return StreamingHttpResponse(iter([b"chunk1", b"chunk2"]), content_type="text/plain")


def whoami(request: HttpRequest) -> HttpResponse:
    set_consumer("tester", name="Tester", group="Testers")
    return HttpResponse("ok")


def error(request: HttpRequest) -> HttpResponse:
    raise ValueError("boom")


class NotesView(View):
    def get(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok")

    def post(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse(status=201)


urlpatterns = [
    path("items/<int:pk>/", get_item),
    path("items/", create_item),
    path("stream/", stream),
    path("whoami/", whoami),
    path("error/", error),
    path("notes/", NotesView.as_view()),
    path("api/", include([path("things/<int:pk>/", get_item)])),
]
