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
    """Config refresh and redaction-aware body attribute writing shared by the transport middlewares."""

    config: ApitallyConfig | None
    redaction: Redaction

    def refresh_config(self) -> ApitallyConfig:
        # Must never raise: it runs at request entry, outside the per-request try/except
        try:
            config = get_config() or ApitallyConfig()
            if config is not self.config:
                self.config = config
                self.redaction = Redaction(config.mask_query_params, config.mask_headers, config.mask_body_fields)
            return config
        except Exception:
            logger.exception("Error refreshing Apitally config")
            return self.config or ApitallyConfig()

    def set_body_attribute(
        self,
        span: Span,
        key: str,
        body: bytes | str,
        mask_callback: Callable[[ReadableSpan, bytes], bytes | None] | None,
        callback_name: str,
    ) -> None:
        if isinstance(body, str):
            span.set_attribute(key, body)
            return
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
                span.set_attribute(key, REDACTED)
                return
            if len(masked) > MAX_BODY_SIZE:
                span.set_attribute(key, BODY_TOO_LARGE)
                return
            body = masked
        try:
            data = json.loads(body)
        except Exception:
            # Non-JSON but allowlisted (e.g. text/plain): stored as-is (design.md section 6)
            span.set_attribute(key, body.decode("utf-8", errors="replace"))
            return
        try:
            value = json.dumps(self.redaction.redact_body(data), separators=(",", ":"), ensure_ascii=False)
        except Exception:
            # Fail closed: privacy over fidelity when redaction breaks on parsed JSON
            logger.warning("Error redacting body, replaced with %s", REDACTED, exc_info=True)
            value = REDACTED
        span.set_attribute(key, value)
