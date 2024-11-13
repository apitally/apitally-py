def test_validation_error_counter():
    from apitally.client.validation_errors import ValidationErrorCounter

    validation_errors = ValidationErrorCounter()
    validation_errors.add_validation_errors(
        consumer=None,
        method="GET",
        path="/test",
        detail=[
            {
                "loc": ["query", "foo"],
                "type": "type_error.integer",
                "msg": "value is not a valid integer",
            },
            {
                "loc": ["query", "bar"],
                "type": "type_error.integer",
                "msg": "value is not a valid integer",
            },
        ],
    )
    validation_errors.add_validation_errors(
        consumer=None,
        method="GET",
        path="/test",
        detail=[
            {
                "loc": ["query", "foo"],
                "type": "type_error.integer",
                "msg": "value is not a valid integer",
            }
        ],
    )

    data = validation_errors.get_and_reset_validation_errors()
    assert len(validation_errors.error_counts) == 0
    assert len(data) == 2
    assert data[0]["method"] == "GET"
    assert data[0]["path"] == "/test"
    assert data[0]["loc"] == ("query", "foo")
    assert data[0]["type"] == "type_error.integer"
    assert data[0]["msg"] == "value is not a valid integer"
    assert data[0]["error_count"] == 2
