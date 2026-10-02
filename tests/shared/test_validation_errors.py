import gzip

from apitally.shared import validation_errors
from apitally.shared.config import MAX_BODY_SIZE
from apitally.shared.validation_errors import ValidationError


def test_validation_errors_are_recorded_aggregated_and_drained() -> None:
    body = gzip.compress(
        b'{"detail":['
        b'{"loc":["body","user",0,"email"],"msg":"required","type":"missing"},'
        b'{"loc":["querystring","page"],"msg":"invalid","type":"int_parsing"}'
        b"]}"
    )
    for consumer in ("consumer", "consumer", None):
        validation_errors.record_validation_response(
            consumer,
            "post",
            "/items",
            body,
            "Application/Problem+JSON; charset=utf-8",
            b"gzip",
            validation_errors.extract_pydantic_validation_errors,
        )

    assert validation_errors.drain_validation_errors() == [
        {
            "method": "POST",
            "path": "/items",
            "source": "body",
            "field": "user.0.email",
            "message": "required",
            "type": "missing",
            "counts": [{"consumer": "consumer", "count": 2}, {"count": 1}],
        },
        {
            "method": "POST",
            "path": "/items",
            "source": "query",
            "field": "page",
            "message": "invalid",
            "type": "int_parsing",
            "counts": [{"consumer": "consumer", "count": 2}, {"count": 1}],
        },
    ]


def test_validation_response_rejects_ineligible_or_unreadable_body() -> None:
    body = b'{"detail":[{"loc":["body","name"],"msg":"required","type":"missing"}]}'
    oversized_body = b'{"padding":"' + b"x" * MAX_BODY_SIZE + b'"}'
    cases = [
        ("OPTIONS", "/items", "application/json", body, None),
        ("POST", None, "application/json", body, None),
        ("POST", "/items", "text/plain", body, None),
        ("POST", "/items", "application/json", b"{", None),
        ("POST", "/items", "application/json", body, "br"),
        ("POST", "/items", "application/json", oversized_body, None),
        ("POST", "/items", "application/json", gzip.compress(oversized_body), "gzip"),
    ]
    extractor = validation_errors.extract_pydantic_validation_errors
    for method, path, content_type, response_body, content_encoding in cases:
        validation_errors.record_validation_response(
            None,
            method,
            path,
            response_body,
            content_type,
            content_encoding,
            extractor,
        )
    assert validation_errors.drain_validation_errors() == []


def test_distinct_validation_errors_and_fields_are_bounded() -> None:
    prefix = "é"
    long_error = ValidationError(prefix * 33, prefix * 2_049, prefix * 2_049, prefix * 129)
    validation_errors.add_validation_errors(None, "POST", "/items", [long_error])
    (body,) = validation_errors.drain_validation_errors()
    assert len(body["source"]) == validation_errors.MAX_SOURCE_LENGTH
    assert len(body["field"]) == validation_errors.MAX_FIELD_LENGTH
    assert len(body["message"]) == validation_errors.MAX_MESSAGE_LENGTH
    assert len(body["type"]) == validation_errors.MAX_TYPE_LENGTH

    for index in range(validation_errors.MAX_ERRORS + 1):
        error = ValidationError("query", str(index), "invalid", "")
        validation_errors.add_validation_errors(None, "GET", "/items", [error])
    consumers = [f"consumer-{index}" for index in range(validation_errors.MAX_ERRORS + 1)]
    for consumer in consumers:
        error = ValidationError("query", "0", "invalid", "")
        validation_errors.add_validation_errors(consumer, "GET", "/items", [error])

    events = validation_errors.drain_validation_errors()
    assert len(events) == validation_errors.MAX_ERRORS
    assert next(event for event in events if event["field"] == "0")["counts"] == [
        {"count": 1},
        *({"consumer": consumer, "count": 1} for consumer in consumers),
    ]
