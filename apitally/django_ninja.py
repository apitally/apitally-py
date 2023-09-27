from __future__ import annotations

from typing import TYPE_CHECKING, Callable, List, Optional

from ninja.security import APIKeyHeader

from apitally.client.asyncio import ApitallyClient
from apitally.client.base import KeyInfo
from apitally.django import ApitallyMiddleware as _ApitallyMiddleware
from apitally.django import DjangoViewInfo


if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse
    from ninja import NinjaAPI


__all__ = ["ApitallyMiddleware", "APIKeyAuth", "APIKeyAuthBase", "KeyInfo"]


class ApitallyMiddleware(_ApitallyMiddleware):
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        super().__init__(get_response)
        if self.client.sync_api_keys:
            api = _get_api(self.views)
            _add_exception_handlers(api)


class AuthError(Exception):
    pass


class InvalidAPIKey(AuthError):
    pass


class PermissionDenied(AuthError):
    pass


class APIKeyAuthBase(APIKeyHeader):
    def __init__(self, scopes: Optional[List[str]] = None) -> None:
        self.scopes = scopes or []

    def authenticate(self, request: HttpRequest, key: Optional[str]) -> Optional[KeyInfo]:
        if key is None:
            return None
        if self.param_name == "Authorization":
            scheme, _, api_key = key.partition(" ")
            if scheme.lower() != "apikey":
                return None
        else:
            api_key = key
        key_info = ApitallyClient.get_instance().key_registry.get(api_key)
        if key_info is None:
            raise InvalidAPIKey()
        if not key_info.has_scopes(self.scopes):
            raise PermissionDenied()
        return key_info


class APIKeyAuth(APIKeyAuthBase):
    param_name = "Authorization"
    openapi_description = "Provide your API key using the <code>Authorization</code> header and the scheme prefix <code>ApiKey</code>.<br>Example: <pre>Authorization: ApiKey your_api_key_here</pre>"


def _get_api(views: List[DjangoViewInfo]) -> NinjaAPI:
    try:
        return next(
            (view.func.__self__.api for view in views if view.is_ninja_path_view and hasattr(view.func, "__self__"))
        )
    except StopIteration:  # pragma: no cover
        raise RuntimeError("Could not find NinjaAPI instance")


def _add_exception_handlers(api: NinjaAPI) -> None:
    def on_invalid_api_key(request: HttpRequest, exc) -> HttpResponse:
        return api.create_response(request, {"detail": "Invalid API key"}, status=403)

    def on_permission_denied(request: HttpRequest, exc) -> HttpResponse:
        return api.create_response(request, {"detail": "Permission denied"}, status=403)

    api.add_exception_handler(InvalidAPIKey, on_invalid_api_key)
    api.add_exception_handler(PermissionDenied, on_permission_denied)
