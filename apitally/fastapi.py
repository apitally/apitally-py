from typing import Optional

from fastapi.exceptions import HTTPException
from fastapi.openapi.models import APIKey, APIKeyIn
from fastapi.requests import Request
from fastapi.security import SecurityScopes
from fastapi.security.base import SecurityBase
from fastapi.security.utils import get_authorization_scheme_param
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN

from apitally.client.asyncio import ApitallyClient
from apitally.client.base import KeyInfo
from apitally.starlette import ApitallyMiddleware


__all__ = ["ApitallyMiddleware", "APIKeyAuth", "KeyInfo", "api_key_auth"]


class APIKeyAuth(SecurityBase):
    def __init__(self, *, custom_header: Optional[str] = None, auto_error: bool = True) -> None:
        self.model: APIKey
        if custom_header is None:
            self.model = APIKey(
                **{"in": APIKeyIn.header},  # type: ignore[arg-type]
                name="Authorization",
                description="Provide your API key using the <code>Authorization</code> header and the scheme prefix <code>ApiKey</code>.<br>Example: <pre>Authorization: ApiKey your_api_key_here</pre>",
            )
            self.scheme_name = "Authorization header with ApiKey scheme"
        else:
            self.model = APIKey(
                **{"in": APIKeyIn.header},  # type: ignore[arg-type]
                name=custom_header,
                description=f"Provide your API key using the <code>{custom_header}</code> header.<br>Example: <pre>{custom_header}: your_api_key_here</pre>",
            )
            self.scheme_name = f"{custom_header} header"
        self.auto_error = auto_error

    async def __call__(self, request: Request, security_scopes: SecurityScopes) -> Optional[KeyInfo]:
        header = request.headers.get(self.model.name)
        if self.model.name == "Authorization":
            scheme, api_key = get_authorization_scheme_param(header)
            if not header or scheme.lower() != "apikey":
                if self.auto_error:
                    raise HTTPException(
                        status_code=HTTP_401_UNAUTHORIZED,
                        detail="Not authenticated" if not header else "Invalid authorization scheme",
                        headers={"WWW-Authenticate": "ApiKey"},
                    )
                else:
                    return None  # pragma: no cover
        elif not header:
            if self.auto_error:
                raise HTTPException(
                    status_code=HTTP_403_FORBIDDEN,
                    detail="Missing API key",
                )
            else:
                return None  # pragma: no cover
        else:
            api_key = header
        key_info = ApitallyClient.get_instance().key_registry.get(api_key)
        if key_info is None and self.auto_error:
            raise HTTPException(
                status_code=HTTP_403_FORBIDDEN,
                detail="Invalid API key",
            )
        if key_info is not None and self.auto_error and not key_info.has_scopes(security_scopes.scopes):
            raise HTTPException(
                status_code=HTTP_403_FORBIDDEN,
                detail="Permission denied",
            )
        if key_info is not None:
            request.state.key_info = key_info
        return key_info


api_key_auth = APIKeyAuth()
