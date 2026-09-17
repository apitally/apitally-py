from django.urls import include, path

from tests.django.rest_framework_urls import api_router


urlpatterns = [path("api/", include(api_router.urls))]
