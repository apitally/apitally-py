from __future__ import annotations

from typing import Any

from django.urls import URLPattern, URLResolver, get_resolver
from ninja import NinjaAPI

from apitally.django import init_apitally


__all__ = ["init_apitally"]


def _get_ninja_paths(urlconfs: list[str | None]) -> list[dict[str, str]]:
    paths = []
    for api in _get_ninja_api_instances(urlconfs=urlconfs):
        schema = api.get_openapi_schema()
        for path, operations in schema["paths"].items():
            for method, operation in operations.items():
                if method.upper() in ("HEAD", "OPTIONS"):
                    continue
                item = {"method": method.upper(), "path": path}
                if operation.get("summary"):
                    item["summary"] = operation["summary"]
                if operation.get("description"):
                    item["description"] = operation["description"]
                paths.append(item)
    return paths


def _get_ninja_schema(urlconfs: list[str | None]) -> dict[str, Any] | None:
    schemas = []
    for api in _get_ninja_api_instances(urlconfs=urlconfs):
        schema = api.get_openapi_schema()
        if len(schema["paths"]) > 0:
            schemas.append(schema)
    return schemas[0] if len(schemas) == 1 else None


def _get_ninja_api_instances(
    urlconfs: list[str | None] | None = None, patterns: list[Any] | None = None
) -> set[NinjaAPI]:
    if urlconfs is None:
        urlconfs = [None]
    if patterns is None:
        patterns = []
        for urlconf in urlconfs:
            patterns.extend(get_resolver(urlconf).url_patterns)
    apis: set[NinjaAPI] = set()
    for p in patterns:
        if isinstance(p, URLResolver):
            if p.app_name != "ninja":
                apis.update(_get_ninja_api_instances(patterns=p.url_patterns))
            else:
                for pattern in p.url_patterns:
                    if isinstance(pattern, URLPattern) and pattern.lookup_str.startswith("ninja."):
                        callback_keywords = getattr(pattern.callback, "keywords", {})
                        if isinstance(callback_keywords, dict):
                            api = callback_keywords.get("api")
                            if isinstance(api, NinjaAPI):
                                apis.add(api)
                                break
    return apis
