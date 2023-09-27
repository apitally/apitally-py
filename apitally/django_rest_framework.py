from __future__ import annotations

from typing import TYPE_CHECKING, List, Type

from django.conf import settings
from rest_framework.permissions import BasePermission

from apitally.client.base import KeyInfo
from apitally.client.threading import ApitallyClient
from apitally.django import ApitallyMiddleware


if TYPE_CHECKING:
    from rest_framework.request import Request
    from rest_framework.views import APIView


__all__ = ["ApitallyMiddleware", "HasAPIKey", "HasAPIKeyWithScopes", "KeyInfo"]


class HasAPIKey(BasePermission):  # type: ignore[misc]
    required_scopes: List[str] = []

    def has_permission(self, request: Request, view: APIView) -> bool:
        custom_header = getattr(settings, "APITALLY_CUSTOM_API_KEY_HEADER", None)
        header = request.headers.get("Authorization" if custom_header is None else custom_header)
        if not header:
            return False
        if custom_header is None:
            scheme, _, api_key = header.partition(" ")
            if scheme.lower() != "apikey":
                return False
        else:
            api_key = header
        key_info = ApitallyClient.get_instance().key_registry.get(api_key)
        if key_info is None:
            return False
        if self.required_scopes and not key_info.has_scopes(self.required_scopes):
            return False
        request.auth = key_info
        return True


def HasAPIKeyWithScopes(scopes: List[str]) -> Type[HasAPIKey]:
    class _HasAPIKeyWithScopes(HasAPIKey):  # type: ignore[misc]
        required_scopes = scopes

    return _HasAPIKeyWithScopes
