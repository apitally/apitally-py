from __future__ import annotations

from typing import TYPE_CHECKING, List, Type

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
        authorization = request.headers.get("Authorization")
        if not authorization:
            return False
        scheme, _, param = authorization.partition(" ")
        if scheme.lower() != "apikey":
            return False
        key_info = ApitallyClient.get_instance().key_registry.get(param)
        if key_info is None:
            return False
        if self.required_scopes and not key_info.check_scopes(self.required_scopes):
            return False
        request.auth = key_info
        return True


def HasAPIKeyWithScopes(scopes: List[str]) -> Type[HasAPIKey]:
    class _HasAPIKeyWithScopes(HasAPIKey):  # type: ignore[misc]
        required_scopes = scopes

    return _HasAPIKeyWithScopes
