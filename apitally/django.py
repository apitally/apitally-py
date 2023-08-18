from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import django
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.urls import resolve

import apitally
from apitally.client.threading import ApitallyClient


__all__ = ["ApitallyMiddleware"]


@dataclass
class ApitallyMiddlewareConfig:
    client_id: str
    env: str
    app_version: Optional[str]
    enable_keys: bool
    sync_interval: float
    openapi_url: Optional[str]


class ApitallyMiddleware:
    config: Optional[ApitallyMiddlewareConfig] = None

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        if self.config is None:
            config = getattr(settings, "APITALLY_MIDDLEWARE", {})
            self.configure(**config)
            assert self.config is not None
        self.client = ApitallyClient(
            client_id=self.config.client_id,
            env=self.config.env,
            enable_keys=self.config.enable_keys,
            sync_interval=self.config.sync_interval,
        )
        self.client.start_sync_loop()
        self.client.send_app_info(app_info=_get_app_info(self.config.app_version))

    @classmethod
    def configure(
        cls,
        client_id: str,
        env: str = "default",
        app_version: Optional[str] = None,
        enable_keys: bool = False,
        sync_interval: float = 60,
        openapi_url: Optional[str] = "/openapi.json",
    ) -> None:
        cls.config = ApitallyMiddlewareConfig(
            client_id=client_id,
            env=env,
            app_version=app_version,
            enable_keys=enable_keys,
            sync_interval=sync_interval,
            openapi_url=openapi_url,
        )

    def __call__(self, request: HttpRequest) -> HttpResponse:
        resolver_match = resolve(request.path_info)
        start_time = time.perf_counter()
        response = self.get_response(request)
        if request.method is not None:
            self.client.request_logger.log_request(
                method=request.method,
                path=resolver_match.route,
                status_code=response.status_code,
                response_time=time.perf_counter() - start_time,
            )
        return response


def _get_app_info(app_version: Optional[str]) -> Dict[str, Any]:
    return {
        "versions": _get_versions(app_version),
        "client": "apitally-python",
        "framework": "django",
    }


def _get_versions(app_version: Optional[str]) -> Dict[str, str]:
    versions = {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "apitally": apitally.__version__,
        "django": django.__version__,
    }
    try:
        import rest_framework

        versions["django-rest-framework"] = rest_framework.__version__  # type: ignore[attr-defined]
    except (ImportError, AttributeError):  # pragma: no cover
        pass
    try:
        import ninja

        versions["django-ninja"] = ninja.__version__
    except (ImportError, AttributeError):  # pragma: no cover
        pass
    if app_version:
        versions["app"] = app_version
    return versions
