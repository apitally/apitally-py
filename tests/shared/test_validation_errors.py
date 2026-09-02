import gzip
import threading

import pytest

from apitally.shared import validation_errors
from apitally.shared.config import MAX_BODY_SIZE
from apitally.shared.validation_errors import ValidationError


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        (["body", "user", 0, "email"], ("body", "user.0.email")),
        (["querystring", "page"], ("query", "page")),
        (["params", "item_id"], ("path", "item_id")),
        (["headers", "x-token"], ("header", "x-token")),
        (["cookies", "session"], ("cookie", "session")),
        (["model", "query", 1, True, None], ("", "model.query.1")),
        ("body.email", ("", "")),
        ([], ("", "")),
    ],
)
def test_format_location_uses_only_the_first_segment_as_source(location: object, expected: tuple[str, str]) -> None:
    assert validation_errors.format_location(location) == expected


def test_extract_pydantic_validation_errors_accepts_only_detail_list_fields() -> None:
    assert validation_errors.extract_pydantic_validation_errors(
        {
            "detail": [
                {"loc": ["path", "item_id"], "msg": "invalid", "type": "int_parsing", "input": "x"},
                {"loc": ["custom", "query", "value"], "msg": 1, "type": None, "ctx": {}},
                {},
                {"message": "ignored"},
                "ignored",
            ]
        }
    ) == [
        ValidationError("path", "item_id", "invalid", "int_parsing"),
        ValidationError("", "custom.query.value", "", ""),
    ]
    assert validation_errors.extract_pydantic_validation_errors({"detail": {}}) == []
    assert validation_errors.extract_pydantic_validation_errors([]) == []


def test_equal_validation_errors_drain_with_summed_count_and_required_empty_fields() -> None:
    error = ValidationError("", "field", "invalid", "")
    validation_errors.add_validation_errors("consumer", "post", "/items/{item_id}", [error, error])
    validation_errors.add_validation_errors("consumer", "POST", "/items/{item_id}", [error])
    validation_errors.add_validation_errors(None, "POST", "/items/{item_id}", [error])

    assert validation_errors.drain_validation_errors() == [
        {
            "consumer": "consumer",
            "method": "POST",
            "path": "/items/{item_id}",
            "source": "",
            "field": "field",
            "message": "invalid",
            "type": "",
            "count": 3,
        },
        {
            "method": "POST",
            "path": "/items/{item_id}",
            "source": "",
            "field": "field",
            "message": "invalid",
            "type": "",
            "count": 1,
        },
    ]


def test_each_validation_error_identity_field_separates_groups() -> None:
    base = ValidationError("body", "name", "invalid", "value")
    variants = [
        ("other", "POST", "/items", base),
        (None, "PUT", "/items", base),
        (None, "POST", "/other", base),
        (None, "POST", "/items", ValidationError("query", "name", "invalid", "value")),
        (None, "POST", "/items", ValidationError("body", "other", "invalid", "value")),
        (None, "POST", "/items", ValidationError("body", "name", "other", "value")),
        (None, "POST", "/items", ValidationError("body", "name", "invalid", "other")),
    ]
    for consumer, method, path, error in variants:
        validation_errors.add_validation_errors(consumer, method, path, [error])
    assert len(validation_errors.drain_validation_errors()) == len(variants)


def test_character_limits_are_applied_before_validation_error_grouping() -> None:
    prefix = "é"
    first = ValidationError(prefix * 32 + "a", prefix * 2_048 + "a", prefix * 2_048 + "a", prefix * 128 + "a")
    second = ValidationError(prefix * 32 + "b", prefix * 2_048 + "b", prefix * 2_048 + "b", prefix * 128 + "b")
    validation_errors.add_validation_errors(None, prefix * 12 + "a", prefix * 2_000 + "a", [first])
    validation_errors.add_validation_errors(None, prefix * 12 + "b", prefix * 2_000 + "b", [second])

    (body,) = validation_errors.drain_validation_errors()
    assert body == {
        "method": prefix.upper() * 12,
        "path": prefix * 2_000,
        "source": prefix * 32,
        "field": prefix * 2_048,
        "message": prefix * 2_048,
        "type": prefix * 128,
        "count": 2,
    }


def test_validation_error_identity_cap_keeps_incrementing_retained_groups() -> None:
    for index in range(validation_errors.MAX_GROUPS + 1):
        validation_errors.add_validation_errors(None, "GET", "/items", [ValidationError("query", str(index), "x", "")])
    retained = ValidationError("query", "0", "x", "")
    validation_errors.add_validation_errors(None, "GET", "/items", [retained])

    events = validation_errors.drain_validation_errors()
    assert len(events) == validation_errors.MAX_GROUPS
    assert next(event for event in events if event["field"] == "0")["count"] == 2
    assert all(event["field"] != str(validation_errors.MAX_GROUPS) for event in events)


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("application/json", True),
        ("Application/Problem+JSON; charset=utf-8", True),
        (b"application/vnd.api+json", True),
        ("application/x-ndjson", False),
        ("text/json", False),
        (None, False),
    ],
)
def test_json_content_type_recognition(content_type: str | bytes | None, expected: bool) -> None:
    assert validation_errors.is_json_content_type(content_type) is expected


def test_json_response_decoding_supports_identity_and_bounded_gzip() -> None:
    body = b'{"detail": []}'
    assert validation_errors.decode_json_response(body, None) == {"detail": []}
    assert validation_errors.decode_json_response(body, "identity") == {"detail": []}
    assert validation_errors.decode_json_response(gzip.compress(body), b"gzip") == {"detail": []}
    assert validation_errors.decode_json_response(b"{", None) is None
    assert validation_errors.decode_json_response(body, "br") is None
    assert validation_errors.decode_json_response(b"not gzip", "gzip") is None
    assert validation_errors.decode_json_response(gzip.compress(body)[:-2], "gzip") is None
    oversized_json = b'{"padding":"' + b"x" * MAX_BODY_SIZE + b'"}'
    assert validation_errors.decode_json_response(oversized_json, None) is None
    assert validation_errors.decode_json_response(gzip.compress(oversized_json), "gzip") is None


def test_concurrent_validation_add_and_drain_preserves_occurrence_count() -> None:
    error = ValidationError("body", "name", "required", "missing")
    start = threading.Event()

    def add_errors() -> None:
        start.wait()
        for _ in range(1_000):
            validation_errors.add_validation_errors(None, "POST", "/items", [error])

    threads = [threading.Thread(target=add_errors) for _ in range(4)]
    for thread in threads:
        thread.start()
    start.set()
    count = 0
    while any(thread.is_alive() for thread in threads):
        count += sum(event["count"] for event in validation_errors.drain_validation_errors())
    for thread in threads:
        thread.join()
    count += sum(event["count"] for event in validation_errors.drain_validation_errors())
    assert count == 4_000
