import sys
from typing import Any, Dict, List, Optional

import starlette
from httpx import HTTPStatusError
from starlette.routing import BaseRoute, Router
from starlette.schemas import EndpointInfo, SchemaGenerator
from starlette.testclient import TestClient
from starlette.types import ASGIApp

import starlette_apitally


def get_app_info(app: ASGIApp, openapi_url: Optional[str]) -> Dict[str, Any]:
    if schema := get_openapi_schema(app, openapi_url):
        info = {
            "title": schema["info"].get("title"),
            "summary": schema["info"].get("summary"),
            "description": schema["info"].get("description"),
            "version": schema["info"].get("version"),
            "paths": transform_openapi_paths(schema["paths"]),
        }
    elif endpoints := get_endpoint_info(app):
        info = {
            "paths": [{"path": endpoint.path, "method": endpoint.http_method} for endpoint in endpoints],
        }
    info["client_version"] = starlette_apitally.__version__
    info["starlette_version"] = starlette.__version__
    info["python_version"] = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return info


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


def transform_openapi_paths(paths: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            "path": path,
            "method": method.upper(),
            "summary": operation.get("summary") or path_item.get("summary"),
            "description": operation.get("description") or path_item.get("description"),
            "responses": {
                status_code: response.get("description", "")
                for status_code, response in operation.get("responses", {}).items()
            },
        }
        for path, path_item in paths.items()
        for method, operation in path_item.items()
    ]


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
