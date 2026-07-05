import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode


REDACTED = "[REDACTED]"

DEFAULT_QUERY_PARAM_PATTERNS = [
    r"auth",
    r"api-?key",
    r"secret",
    r"token",
    r"password",
    r"pwd",
]
DEFAULT_HEADER_PATTERNS = [
    r"auth",
    r"api-?key",
    r"secret",
    r"token",
    r"cookie",
]
DEFAULT_BODY_FIELD_PATTERNS = [
    r"password",
    r"pwd",
    r"token",
    r"secret",
    r"auth",
    r"card[-_ ]?number",
    r"ccv",
    r"ssn",
]


class Redaction:
    def __init__(
        self,
        query_params: list[str] | None = None,
        headers: list[str] | None = None,
        body_fields: list[str] | None = None,
    ) -> None:
        self.query_param_patterns = compile_patterns(DEFAULT_QUERY_PARAM_PATTERNS + (query_params or []))
        self.header_patterns = compile_patterns(DEFAULT_HEADER_PATTERNS + (headers or []))
        self.body_field_patterns = compile_patterns(DEFAULT_BODY_FIELD_PATTERNS + (body_fields or []))

    def redact_query_params(self, value: str, assume_query: bool = True) -> str:
        """Redact matching param names in a path?query target, a full URL, or (with assume_query)
        a bare query string."""
        base, sep, query = value.partition("?")
        if not sep:
            if not assume_query:
                return value
            base, query = "", value
        # Legacy semicolon separators would otherwise smuggle values past redaction
        pairs = parse_qsl(query.replace(";", "&"), keep_blank_values=True)
        redacted = urlencode([(k, REDACTED if matches_any(self.query_param_patterns, k) else v) for k, v in pairs])
        return f"{base}?{redacted}" if sep else redacted

    def redact_headers(self, headers: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in headers.items():
            if self.should_redact_header(name):
                value = REDACTED if isinstance(value, str) else [REDACTED]
            result[name] = value
        return result

    def redact_body(self, data: Any) -> Any:
        if isinstance(data, dict):
            return {
                k: REDACTED if isinstance(v, str) and matches_any(self.body_field_patterns, k) else self.redact_body(v)
                for k, v in data.items()
            }
        if isinstance(data, list):
            return [self.redact_body(item) for item in data]
        return data

    def should_redact_header(self, name: str) -> bool:
        # Also match the underscore-normalized attribute key form emitted by older instrumentors
        return matches_any(self.header_patterns, name) or matches_any(self.header_patterns, name.replace("_", "-"))


def compile_patterns(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


def matches_any(patterns: list[re.Pattern[str]], value: str) -> bool:
    return any(pattern.search(value) for pattern in patterns)
