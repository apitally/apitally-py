import re
from typing import Optional
from uuid import UUID


def validate_client_params(client_id: str, env: str, app_version: Optional[str], sync_interval: float) -> None:
    try:
        UUID(client_id)
    except ValueError:
        raise ValueError(f"invalid client_id '{client_id}' (expected hexadecimal UUID format)")
    if re.match(r"^[\w-]{1,32}$", env) is None:
        raise ValueError(f"invalid env '{env}' (expected 1-32 alphanumeric lowercase characters and hyphens only)")
    if app_version is not None and len(app_version) > 32:
        raise ValueError(f"invalid app_version '{app_version}' (expected 1-32 characters)")
    if sync_interval < 10:
        raise ValueError("sync_interval has to be greater or equal to 10 seconds")
