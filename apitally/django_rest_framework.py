from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from rest_framework.schemas.generators import EndpointEnumerator

from apitally.django import init_apitally


__all__ = ["init_apitally"]


def _get_drf_paths(urlconfs: list[str | None]) -> list[dict[str, str]]:
    enumerators = [EndpointEnumerator(urlconf=urlconf) for urlconf in urlconfs]
    return [
        {"method": method.upper(), "path": path}
        for enumerator in enumerators
        for path, method, _ in enumerator.get_api_endpoints()
        if method not in ("HEAD", "OPTIONS")
    ]


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
