import json
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from django.conf import settings
from django.utils.module_loading import import_string

from apitally.django import init


__all__ = ["init"]


def _get_drf_paths(urlconfs: list[str | None]) -> list[dict[str, str]]:
    from rest_framework.schemas.generators import EndpointEnumerator

    enumerators = [EndpointEnumerator(urlconf=urlconf) for urlconf in urlconfs]
    return [
        {"method": method.upper(), "path": path}
        for enumerator in enumerators
        for path, method, _ in enumerator.get_api_endpoints()
        if method not in ("HEAD", "OPTIONS")
    ]


def _get_drf_openapi(urlconfs: list[str | None]) -> str | None:
    from rest_framework.utils.encoders import JSONEncoder

    schema = _get_drf_spectacular_schema(urlconfs) if _uses_drf_spectacular() else _get_drf_schema(urlconfs)
    return json.dumps(schema, cls=JSONEncoder) if schema is not None else None


def _uses_drf_spectacular() -> bool:
    schema_class = getattr(settings, "REST_FRAMEWORK", {}).get("DEFAULT_SCHEMA_CLASS", "")
    with suppress(ImportError):
        from drf_spectacular.openapi import AutoSchema

        return issubclass(import_string(schema_class), AutoSchema)
    return False


def _get_drf_schema(urlconfs: list[str | None]) -> Mapping[str, Any] | None:
    from rest_framework.schemas.openapi import SchemaGenerator

    schemas: list[Mapping[str, Any]] = []
    # AssertionError: uritemplate or inflection not installed; AttributeError: deprecated CoreAPI schema in use
    with suppress(AssertionError, AttributeError):
        for urlconf in urlconfs:
            generator = SchemaGenerator(urlconf=urlconf)
            schema = generator.get_schema()
            if schema is not None and len(schema["paths"]) > 0:
                schemas.append(schema)
    return schemas[0] if len(schemas) == 1 else None


def _get_drf_spectacular_schema(urlconfs: list[str | None]) -> Mapping[str, Any] | None:
    from drf_spectacular.generators import SchemaGenerator

    schemas: list[Mapping[str, Any]] = []
    for urlconf in urlconfs:
        generator = SchemaGenerator(urlconf=urlconf)
        schema = generator.get_schema()
        if schema is not None and len(schema["paths"]) > 0:
            schemas.append(schema)
    return schemas[0] if len(schemas) == 1 else None
