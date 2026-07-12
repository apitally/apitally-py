from __future__ import annotations

from apitally.shared.config import ApitallyConfig, get_config
from apitally.shared.redaction import Redaction


MAX_BODY_SIZE = 50_000
BODY_TOO_LARGE = "[BODY_TOO_LARGE]"
ALLOWED_CONTENT_TYPES = (
    "application/json",
    "application/problem+json",
    "application/vnd.api+json",
    "application/ld+json",
    "application/x-ndjson",
    "text/markdown",
    "text/plain",
)


def is_allowed_content_type(content_type: str | None) -> bool:
    return content_type is not None and content_type.lower().startswith(ALLOWED_CONTENT_TYPES)


class CaptureMixin:
    """Config and redaction binding shared by the transport middlewares and the span exporter."""

    config: ApitallyConfig
    redaction: Redaction

    def bind_config(self) -> None:
        self.config = get_config() or ApitallyConfig()
        self.redaction = Redaction(
            self.config.mask_query_params, self.config.mask_headers, self.config.mask_body_fields
        )
