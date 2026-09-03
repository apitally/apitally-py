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
    for _ in range(2):
        validation_errors.record_validation_response(
            "consumer",
            "post",
            "/items",
            body,
            "Application/Problem+JSON; charset=utf-8",
            b"gzip",
            validation_errors.extract_pydantic_validation_errors,
        )

    assert validation_errors.drain_validation_errors() == [
        {
            "consumer": "consumer",
            "method": "POST",
            "path": "/items",
            "source": "body",
            "field": "user.0.email",
            "message": "required",
            "type": "missing",
            "count": 2,
        },
        {
            "consumer": "consumer",
            "method": "POST",
            "path": "/items",
            "source": "query",
            "field": "page",
            "message": "invalid",
            "type": "int_parsing",
            "count": 2,
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


def test_validation_error_groups_and_fields_are_bounded() -> None:
    prefix = "é"
    long_error = ValidationError(prefix * 33, prefix * 2_049, prefix * 2_049, prefix * 129)
    validation_errors.add_validation_errors(None, "POST", "/items", [long_error])
    (body,) = validation_errors.drain_validation_errors()
    assert len(body["source"]) == validation_errors.MAX_SOURCE_LENGTH
    assert len(body["field"]) == validation_errors.MAX_FIELD_LENGTH
    assert len(body["message"]) == validation_errors.MAX_MESSAGE_LENGTH
    assert len(body["type"]) == validation_errors.MAX_TYPE_LENGTH

    for index in range(validation_errors.MAX_GROUPS + 1):
        error = ValidationError("query", str(index), "invalid", "")
        validation_errors.add_validation_errors(None, "GET", "/items", [error])
    assert len(validation_errors.drain_validation_errors()) == validation_errors.MAX_GROUPS
