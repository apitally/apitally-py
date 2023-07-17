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
    if schema := get_openapi_schema(app, openapi_url):
        info = {}
        if (title := schema["info"].get("title")) and title != "FastAPI":
            info["title"] = title.strip()
        if summary := schema["info"].get("summary"):
            info["summary"] = summary.strip()
        if description := schema["info"].get("description"):
            info["description"] = description.strip()
        if app_version:
            info["version"] = app_version
        elif version := schema["info"].get("version"):
            info["version"] = version.strip()
        if info:
            app_info["info"] = info
        app_info["paths"] = transform_openapi_paths(schema["paths"])
    elif endpoints := get_endpoint_info(app):
        if app_version:
            app_info["info"] = {"version": app_version}
        app_info["paths"] = [{"path": endpoint.path, "method": endpoint.http_method} for endpoint in endpoints]
    app_info["client"] = "starlette-apitally"
    app_info["versions"] = get_versions()
    return app_info


def get_openapi_schema(app: ASGIApp, openapi_url: Optional[str]) -> Optional[Dict[str, Any]]:
    if not openapi_url:
        return None
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(openapi_url)
        response.raise_for_status()
        return response.json()
    except HTTPStatusError:
        return None


def transform_openapi_paths(openapi_paths: Dict[str, Any]) -> List[Dict[str, Any]]:
    paths: List[Dict[str, Any]] = []
    for path, path_item in openapi_paths.items():
        for method, operation in path_item.items():
            item = {"path": path, "method": method}
            if summary := operation.get("summary") or path_item.get("summary"):
                item["summary"] = summary
            if description := operation.get("description") or path_item.get("description"):
                item["description"] = description
            if responses := operation.get("responses"):
                item["responses"] = {
                    status_code: response.get("description", "") for status_code, response in responses.items()
                }
            paths.append(item)
    return paths


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


def get_versions() -> Dict[str, str]:
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
    return versions
