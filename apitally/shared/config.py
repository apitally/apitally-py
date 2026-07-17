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

MAX_BODY_SIZE = 50_000
BODY_TOO_LARGE = b"[BODY_TOO_LARGE]"
ALLOWED_CONTENT_TYPES = (
    "application/json",
    "application/problem+json",
    "application/vnd.api+json",
    "application/ld+json",
    "application/x-ndjson",
    "text/markdown",
    "text/plain",
)


@dataclass
class ApitallyConfig:
    write_token: str = ""
    env: str = "prod"
    disabled: bool = False
    capture_logs: bool = True
    capture_request_headers: bool = False
    capture_request_body: bool = False
    capture_response_headers: bool = True
    capture_response_body: bool = False
    mask_query_params: list[str] = field(default_factory=list)
    mask_headers: list[str] = field(default_factory=list)
    mask_body_fields: list[str] = field(default_factory=list)
    mask_request_body: Callable[[ReadableSpan, bytes], bytes | None] | None = None
    mask_response_body: Callable[[ReadableSpan, bytes], bytes | None] | None = None
    exclude_paths: list[str] = field(default_factory=list)
    sample_rate: float = 1.0
    sample_on_request: Callable[[ReadableSpan], float | bool | None] | None = None
    sample_on_response: Callable[[ReadableSpan], float | bool | None] | None = None
    otlp_endpoint: str = DEFAULT_OTLP_ENDPOINT


CONFIG_FIELDS = frozenset(f.name for f in fields(ApitallyConfig))
PATTERN_FIELDS = ("mask_query_params", "mask_headers", "mask_body_fields", "exclude_paths")

current_config: ApitallyConfig | None = None


def explicit_kwargs(params: dict[str, Any]) -> dict[str, Any]:
    """Filter kwargs passed through an adapter down to config fields the caller actually provided.
    None means absent, which keeps the env var fallbacks in resolve_config in effect."""
    return {name: value for name, value in params.items() if name in CONFIG_FIELDS and value is not None}


def set_config(**kwargs: Any) -> ApitallyConfig:
    global current_config
    config, error = resolve_config(kwargs)
    if current_config is not None:
        if config != current_config:
            logger.warning("apitally.init() was called again with different arguments; ignoring")
        return current_config
    if error:
        logger.error(error)
    current_config = config
    return config


def get_config() -> ApitallyConfig:
    return current_config if current_config is not None else ApitallyConfig()


def is_configured() -> bool:
    return current_config is not None


def reset() -> None:
    global current_config
    current_config = None


def ensure_semconv_opt_in() -> None:
    # The contrib instrumentors read this env var once at first init and cache it for the
    # process; when unset they emit old HTTP semconv names. http/dup adds the stable names
    # without changing anything for a user's existing OTel backend. A user-set value is respected.
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http/dup")


def drop_invalid_patterns(config: ApitallyConfig) -> None:
    for name in PATTERN_FIELDS:
        valid = []
        for pattern in getattr(config, name):
            try:
                re.compile(pattern)
                valid.append(pattern)
            except Exception:
                logger.error("Invalid regex pattern in %s ignored: %r", name, pattern)
        setattr(config, name, valid)


def resolve_config(kwargs: dict[str, Any]) -> tuple[ApitallyConfig, str | None]:
    config = ApitallyConfig(**{k: v for k, v in kwargs.items() if k in CONFIG_FIELDS})
    drop_invalid_patterns(config)
    if not isinstance(config.sample_rate, (int, float)) or not 0 <= config.sample_rate <= 1:
        config.sample_rate = 1.0
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
        elif not isinstance(config.write_token, str) or not WRITE_TOKEN_FORMAT.match(config.write_token):
            error = f"Apitally write token has an invalid format: {str(config.write_token)[:8]}..."
        if error:
            config.disabled = True
    return config, error


def is_allowed_content_type(content_type: str | bytes | None) -> bool:
    if content_type is None:
        return False
    if isinstance(content_type, bytes):
        content_type = content_type.decode("latin-1")
    return content_type.strip().lower().startswith(ALLOWED_CONTENT_TYPES)
