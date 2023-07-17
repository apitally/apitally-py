import sys
from typing import Any, Dict, List, Optional

import starlette
from httpx import HTTPStatusError
from starlette.routing import BaseRoute, Router
from starlette.schemas import EndpointInfo, SchemaGenerator
from starlette.testclient import TestClient
from starlette.types import ASGIApp

import starlette_apitally


def get_app_info(app: ASGIApp, app_version: Optional[str], openapi_url: Optional[str]) -> Dict[str, Any]:
    app_info: Dict[str, Any] = {}
    if openapi := get_openapi(app, openapi_url):
        app_info["openapi"] = openapi
    elif endpoints := get_endpoint_info(app):
        app_info["paths"] = [{"path": endpoint.path, "method": endpoint.http_method} for endpoint in endpoints]
    app_info["versions"] = get_versions(app_version)
    app_info["client"] = "starlette-apitally"
    return app_info


def get_openapi(app: ASGIApp, openapi_url: Optional[str]) -> Optional[str]:
    if not openapi_url:
        return None
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(openapi_url)
        response.raise_for_status()
        return response.text
    except HTTPStatusError:
        return None


def get_endpoint_info(app: ASGIApp) -> List[EndpointInfo]:
    routes = get_routes(app)
    schemas = SchemaGenerator({})
    return schemas.get_endpoints(routes)


def get_routes(app: ASGIApp) -> List[BaseRoute]:
    if isinstance(app, Router):
        return app.routes
    elif hasattr(app, "app"):
        return get_routes(app.app)
    return []


def get_versions(app_version: Optional[str] = None) -> Dict[str, str]:
    versions = {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "starlette-apitally": starlette_apitally.__version__,
        "starlette": starlette.__version__,
    }
    try:
        import fastapi

        versions["fastapi"] = fastapi.__version__
    except (ImportError, AttributeError):
        pass
    if app_version:
        versions["app"] = app_version
    return versions
