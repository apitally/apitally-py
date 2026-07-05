from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan


logger = logging.getLogger(__name__)

DEFAULT_OTLP_ENDPOINT = "https://otlp.apitally.io"
WRITE_TOKEN_FORMAT = re.compile(r"^apt_[a-zA-Z0-9]{24}$")
TRUE_VALUES = frozenset({"1", "true", "yes"})


@dataclass
class ApitallyConfig:
    write_token: str = ""
    env: str = "prod"
    disabled: bool = False
    capture_logs: bool = True
    exclude_on_request: Callable[[ReadableSpan], bool] | None = None
    exclude_on_response: Callable[[ReadableSpan], bool] | None = None
    mask_request_body: Callable[[ReadableSpan, bytes], bytes | None] | None = None
    mask_response_body: Callable[[ReadableSpan, bytes], bytes | None] | None = None
    log_request_headers: bool = False
    log_request_body: bool = False
    log_response_headers: bool = True
    log_response_body: bool = False
    mask_query_params: list[str] = field(default_factory=list)
    mask_headers: list[str] = field(default_factory=list)
    mask_body_fields: list[str] = field(default_factory=list)
    exclude_paths: list[str] = field(default_factory=list)
    otlp_endpoint: str = DEFAULT_OTLP_ENDPOINT


CONFIG_FIELDS = frozenset(f.name for f in fields(ApitallyConfig))

current_config: ApitallyConfig | None = None
fixed_fields: set[str] = set()


def explicit_kwargs(params: dict[str, Any]) -> dict[str, Any]:
    """Filter an adapter signature's locals() down to config fields the caller actually provided;
    None means absent, keeping the env var fallbacks in resolve_config in effect."""
    return {name: value for name, value in params.items() if name in CONFIG_FIELDS and value is not None}


def configure(**kwargs: Any) -> ApitallyConfig:
    global current_config
    config, error = resolve_config(kwargs)
    if current_config is not None:
        if config == current_config:
            return current_config
        for name in fixed_fields:
            if getattr(config, name) != getattr(current_config, name):
                logger.debug("Config field '%s' cannot be changed after activation, keeping previous value", name)
                setattr(config, name, getattr(current_config, name))
    if error:
        logger.error(error)
    current_config = config
    return config


def get_config() -> ApitallyConfig | None:
    return current_config


def mark_fixed(*field_names: str) -> None:
    fixed_fields.update(field_names)


def reset() -> None:
    global current_config
    current_config = None
    fixed_fields.clear()


def ensure_semconv_opt_in() -> None:
    # The contrib instrumentors latch this process-globally at first init and emit old HTTP
    # semconv names when unset; http/dup adds the stable names without changing anything for
    # a cooperative user's own backend. A user-set value is respected.
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http/dup")


def mask_token(token: str) -> str:
    return f"{token[:8]}..."


def resolve_config(kwargs: dict[str, Any]) -> tuple[ApitallyConfig, str | None]:
    config = ApitallyConfig(**{k: v for k, v in kwargs.items() if k in CONFIG_FIELDS})
    if "write_token" not in kwargs and (token := os.environ.get("APITALLY_WRITE_TOKEN")):
        config.write_token = token
    if "env" not in kwargs and (env := os.environ.get("APITALLY_ENV")):
        config.env = env
    if "disabled" not in kwargs:
        value = os.environ.get("APITALLY_DISABLED") or os.environ.get("OTEL_SDK_DISABLED") or ""
        config.disabled = value.strip().lower() in TRUE_VALUES
    if endpoint := os.environ.get("APITALLY_OTLP_ENDPOINT"):
        config.otlp_endpoint = endpoint

    error = None
    if not config.disabled:
        if not config.write_token:
            error = "Apitally write token is missing (set the write_token argument or APITALLY_WRITE_TOKEN)"
        elif not WRITE_TOKEN_FORMAT.match(config.write_token):
            error = f"Apitally write token has an invalid format: {mask_token(config.write_token)}"
        if error:
            config.disabled = True
    return config, error
