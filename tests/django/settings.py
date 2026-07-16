"""Real settings module exercising the documented end-of-settings.py apitally.init() placement."""

import apitally


SECRET_KEY = "secret"
ALLOWED_HOSTS = ["testserver"]
ROOT_URLCONF = "tests.django.urls"
DEBUG = False

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
]

apitally.init(write_token="apt_" + "a" * 24)
