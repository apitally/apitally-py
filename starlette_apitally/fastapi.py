from typing import Optional

from fastapi.exceptions import HTTPException
from fastapi.openapi.models import APIKey, APIKeyIn
from fastapi.requests import Request
from fastapi.security.base import SecurityBase
from fastapi.security.utils import get_authorization_scheme_param
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN

from starlette_apitally.keys import Key, Keys


class AuthorizationAPIKeyHeader(SecurityBase):
    def __init__(self, *, auto_error: bool = True):
        self.model = APIKey(
            **{"in": APIKeyIn.header},  # type: ignore[arg-type]
            name="Authorization",
            description="Provide your API key using the <code>Authorization</code> header and the scheme prefix <code>ApiKey</code>.<br>Example: <pre>Authorization: ApiKey your_api_key_here</pre>",
        )
        self.scheme_name = "Authorization header with ApiKey scheme"
        self.auto_error = auto_error

    async def __call__(self, request: Request) -> Optional[Key]:
        authorization = request.headers.get("Authorization")
        scheme, param = get_authorization_scheme_param(authorization)
        if not authorization or scheme.lower() != "apikey":
            if self.auto_error:
                raise HTTPException(
                    status_code=HTTP_401_UNAUTHORIZED,
                    detail="Not authenticated" if not authorization else "Invalid authorization scheme",
                    headers={"WWW-Authenticate": "ApiKey"},
                )
            else:
                return None
        keys = self._get_keys()
        key = keys.get(param)
        if key is None and self.auto_error:
            raise HTTPException(
                status_code=HTTP_403_FORBIDDEN,
                detail="Invalid API key",
            )
        return key

    def _get_keys(self) -> Keys:
        from starlette_apitally.client import ApitallyClient

        client = ApitallyClient._instance
        if client is None:
            raise RuntimeError("ApitallyClient not initialized")
        return client.keys


api_key_auth = AuthorizationAPIKeyHeader()
