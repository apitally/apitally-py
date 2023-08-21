from __future__ import annotations

from typing import TYPE_CHECKING

from rest_framework.permissions import BasePermission

from apitally.client.base import KeyInfo
from apitally.client.threading import ApitallyClient
from apitally.django import ApitallyMiddleware


if TYPE_CHECKING:
    from django.http import HttpRequest
    from rest_framework.views import APIView


__all__ = ["ApitallyMiddleware", "HasAPIKey", "KeyInfo"]


class HasAPIKey(BasePermission):  # type: ignore[misc]
    def has_permission(self, request: HttpRequest, view: APIView) -> bool:
        authorization = request.headers.get("Authorization")
        if not authorization:
            return False
        scheme, _, param = authorization.partition(" ")
        if scheme.lower() != "apikey":
            return False
        key_info = ApitallyClient.get_instance().key_registry.get(param)
        if key_info is None:
            return False
        if hasattr(view, "required_scopes") and not key_info.check_scopes(view.required_scopes):
            return False
        if not hasattr(request, "key_info"):
            setattr(request, "key_info", key_info)
        return True
