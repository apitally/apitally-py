from urllib.parse import parse_qsl

from apitally.shared.redaction import REDACTED, Redaction


def test_redact_query_params_mixed():
    redaction = Redaction()
    result = redaction.redact_query_params("user=alice&apiKey=abc123&page=2&PASSWORD=hunter2")
    assert dict(parse_qsl(result)) == {
        "user": "alice",
        "apiKey": REDACTED,
        "page": "2",
        "PASSWORD": REDACTED,
    }


def test_redact_query_params_value_shapes():
    redaction = Redaction()
    assert redaction.redact_query_params("/items?secret=1&q=2") == "/items?secret=%5BREDACTED%5D&q=2"
    assert (
        redaction.redact_query_params("https://example.com/items?token=x")
        == "https://example.com/items?token=%5BREDACTED%5D"
    )
    assert redaction.redact_query_params("/items", assume_query=False) == "/items"


def test_redact_query_params_parsing_edge_cases():
    redaction = Redaction()
    # Valueless params are preserved, not dropped
    assert redaction.redact_query_params("/items?debug&x=1", assume_query=False) == "/items?debug=&x=1"
    # Legacy semicolon separators must not smuggle values past redaction
    assert redaction.redact_query_params("a=1;token=x") == "a=1&token=%5BREDACTED%5D"
    # A query-less path containing '=' is not a query string
    assert redaction.redact_query_params("/items/key=value", assume_query=False) == "/items/key=value"


def test_redact_headers():
    redaction = Redaction()
    headers = {
        "content-type": ["application/json"],
        "x-api-key": ["abc"],
        "x_api_key": ["abc"],
        "Authorization": "Bearer xyz",
    }
    assert redaction.redact_headers(headers) == {
        "content-type": ["application/json"],
        "x-api-key": [REDACTED],
        "x_api_key": [REDACTED],
        "Authorization": REDACTED,
    }


def test_redact_body_fields():
    redaction = Redaction()
    data = {"Password": "x", "card_number": "4111", "CardNumber": "4242", "amount": 100, "note": "hi"}
    assert redaction.redact_body(data) == {
        "Password": REDACTED,
        "card_number": REDACTED,
        "CardNumber": REDACTED,
        "amount": 100,
        "note": "hi",
    }


def test_redact_body_walk():
    redaction = Redaction()
    data = {
        "user": {"password": "secret", "age": 30},
        "items": [{"token": "t1"}, {"token": 123}],
        "auth": {"nested": "keep"},
    }
    assert redaction.redact_body(data) == {
        "user": {"password": REDACTED, "age": 30},
        "items": [{"token": REDACTED}, {"token": 123}],
        "auth": {"nested": "keep"},
    }


def test_user_patterns_extend_defaults():
    redaction = Redaction(query_params=["custom"], headers=["x-internal"], body_fields=["nickname"])
    assert dict(parse_qsl(redaction.redact_query_params("custom_id=1&token=2"))) == {
        "custom_id": REDACTED,
        "token": REDACTED,
    }
    assert redaction.redact_headers({"x-internal-id": "1", "cookie": "c"}) == {
        "x-internal-id": REDACTED,
        "cookie": REDACTED,
    }
    assert redaction.redact_body({"nickname": "a", "password": "b"}) == {
        "nickname": REDACTED,
        "password": REDACTED,
    }
