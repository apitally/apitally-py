import re
from collections.abc import Mapping
from functools import lru_cache
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
URL_HEADER_NAMES = frozenset({"location", "content-location"})

JSONValue = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


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
        pairs = parse_qsl(query, keep_blank_values=True)
        redacted = urlencode([(k, REDACTED if self.should_redact_query_param(k) else v) for k, v in pairs])
        return f"{base}?{redacted}" if sep else redacted

    def redact_headers(self, headers: Mapping[str, str | list[str]]) -> dict[str, str | list[str]]:
        result: dict[str, str | list[str]] = {}
        for name, value in headers.items():
            if self.should_redact_header(name):
                value = REDACTED if isinstance(value, str) else [REDACTED]
            elif name.lower() in URL_HEADER_NAMES:
                if isinstance(value, str):
                    value = self.redact_query_params(value, assume_query=False)
                else:
                    value = [self.redact_query_params(item, assume_query=False) for item in value]
            result[name] = value
        return result

    def redact_body(self, data: JSONValue) -> JSONValue:
        if isinstance(data, dict):
            return {
                k: REDACTED if isinstance(v, str) and self.should_redact_body_field(k) else self.redact_body(v)
                for k, v in data.items()
            }
        if isinstance(data, list):
            return [self.redact_body(item) for item in data]
        return data

    @lru_cache(maxsize=128)
    def should_redact_header(self, name: str) -> bool:
        # Also match the underscore-normalized attribute key form emitted by older instrumentors
        return matches_any(self.header_patterns, name) or matches_any(self.header_patterns, name.replace("_", "-"))

    @lru_cache(maxsize=1024)
    def should_redact_query_param(self, name: str) -> bool:
        return matches_any(self.query_param_patterns, name)

    @lru_cache(maxsize=1024)
    def should_redact_body_field(self, name: str) -> bool:
        return matches_any(self.body_field_patterns, name)


def compile_patterns(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


def combine_patterns(patterns: list[str]) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{pattern})" for pattern in patterns), re.IGNORECASE)


def matches_any(patterns: list[re.Pattern[str]], value: str) -> bool:
    return any(pattern.search(value) for pattern in patterns)
