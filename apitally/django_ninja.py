from __future__ import annotations

from typing import List, Optional

from django.http import HttpRequest
from ninja.security import APIKeyHeader

from apitally.client.asyncio import ApitallyClient
from apitally.client.base import KeyInfo
from apitally.django import ApitallyMiddleware


__all__ = [
    "ApitallyMiddleware",
    "AuthorizationAPIKeyHeader",
    "KeyInfo",
    "AuthError",
    "InvalidAPIKey",
    "PermissionDenied",
]


class AuthError(Exception):
    pass


class InvalidAPIKey(AuthError):
    pass


class PermissionDenied(AuthError):
    pass


class AuthorizationAPIKeyHeader(APIKeyHeader):
    param_name = "Authorization"
    openapi_description = "Provide your API key using the <code>Authorization</code> header and the scheme prefix <code>ApiKey</code>.<br>Example: <pre>Authorization: ApiKey your_api_key_here</pre>"

    def __init__(self, scopes: Optional[List[str]] = None) -> None:
        self.scopes = scopes or []

    def authenticate(self, request: HttpRequest, key: Optional[str]) -> Optional[KeyInfo]:
        if key is None:
            return None
        scheme, _, param = key.partition(" ")
        if scheme.lower() != "apikey":
            return None
        key_info = ApitallyClient.get_instance().key_registry.get(param)
        if key_info is None:
            raise InvalidAPIKey()
        if not key_info.check_scopes(self.scopes):
            raise PermissionDenied()
        return key_info
