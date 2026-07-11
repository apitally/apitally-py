from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from apitally.shared.config import ApitallyConfig, get_config
from apitally.shared.redaction import REDACTED, Redaction


if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan, Span


logger = logging.getLogger(__name__)

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
    """Config binding and redaction-aware body attribute writing shared by the transport middlewares."""

    config: ApitallyConfig
    redaction: Redaction

    def bind_config(self) -> None:
        self.config = get_config() or ApitallyConfig()
        self.redaction = Redaction(
            self.config.mask_query_params, self.config.mask_headers, self.config.mask_body_fields
        )

    def set_body_attribute(
        self,
        span: Span,
        key: str,
        body: bytes | str,
        mask_callback: Callable[[ReadableSpan, bytes], bytes | None] | None,
        callback_name: str,
    ) -> None:
        span.set_attribute(key, self.process_body(span, body, mask_callback, callback_name))

    def process_body(
        self,
        span: ReadableSpan,
        body: bytes | str,
        mask_callback: Callable[[ReadableSpan, bytes], bytes | None] | None,
        callback_name: str,
    ) -> str:
        if isinstance(body, str):
            return body
        if mask_callback is not None:
            try:
                masked = mask_callback(span, body)
            except Exception:
                logger.warning(
                    "Apitally %s callback raised an exception, body replaced with %s",
                    callback_name,
                    REDACTED,
                    exc_info=True,
                )
                masked = None
            if masked is None:
                return REDACTED
            if len(masked) > MAX_BODY_SIZE:
                return BODY_TOO_LARGE
            body = masked
        try:
            data = json.loads(body)
        except Exception:
            # Non-JSON but allowlisted (e.g. text/plain): stored as-is
            return body.decode("utf-8", errors="replace")
        try:
            return json.dumps(self.redaction.redact_body(data), separators=(",", ":"), ensure_ascii=False)
        except Exception:
            logger.warning("Error redacting body, replaced with %s", REDACTED, exc_info=True)
            return REDACTED
