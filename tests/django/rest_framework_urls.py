from contextlib import suppress
from decimal import Decimal

from django.urls import include, path
from rest_framework import serializers
from rest_framework.generics import ListCreateAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.routers import SimpleRouter
from rest_framework.viewsets import ViewSet


with suppress(ImportError):
    from drf_spectacular.openapi import AutoSchema

    class CustomAutoSchema(AutoSchema):
        pass


class ItemViewSet(ViewSet):
    def list(self, request: Request) -> Response:
        return Response([{"id": 1}])

    def retrieve(self, request: Request, pk: str | None = None) -> Response:
        return Response({"id": int(pk or 0)})


class ThingViewSet(ViewSet):
    def retrieve(self, request: Request, pk: str | None = None) -> Response:
        return Response({"id": int(pk or 0)})


class PriceSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))


class PriceView(ListCreateAPIView):
    serializer_class = PriceSerializer
    queryset = []


router = SimpleRouter()
router.register("items", ItemViewSet, basename="item")

api_router = SimpleRouter()
api_router.register("things", ThingViewSet, basename="thing")

urlpatterns = [
    path("", include(router.urls)),
    path("api/", include(api_router.urls)),
    path("prices/", PriceView.as_view()),
]
