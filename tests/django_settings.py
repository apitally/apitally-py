"""Real settings module exercising the documented end-of-settings.py init_apitally placement."""

from apitally.django import init_apitally


SECRET_KEY = "secret"
ALLOWED_HOSTS = ["testserver"]
ROOT_URLCONF = "tests.django_urls"
DEBUG = False

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
]

init_apitally(write_token="apt_" + "a" * 24)
