from apitally.shared.config import ApitallyConfig, get_config


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
    """Config binding shared by the transport middlewares."""

    config: ApitallyConfig

    def bind_config(self) -> None:
        self.config = get_config() or ApitallyConfig()
