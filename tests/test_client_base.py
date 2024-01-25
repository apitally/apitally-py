import pytest

from .constants import CLIENT_ID, ENV


def test_request_counter():
    from apitally.client.base import RequestCounter

    requests = RequestCounter()
    requests.add_request(
        consumer=None,
        method="GET",
        path="/test",
        status_code=200,
        response_time=0.105,
        request_size=None,
        response_size="123",
    )
    requests.add_request(
        consumer=None,
        method="GET",
        path="/test",
        status_code=200,
        response_time=0.227,
        request_size=None,
        response_size="321",
    )
    requests.add_request(
        consumer=None,
        method="POST",
        path="/test",
        status_code=204,
        response_time=0.1,
        request_size="123",
        response_size=None,
    )
    assert len(requests.request_counts) == 2

    data = requests.get_and_reset_requests()
    assert len(requests.request_counts) == 0
    assert len(data) == 2
    assert data[0]["method"] == "GET"
    assert data[0]["path"] == "/test"
    assert data[0]["status_code"] == 200
    assert data[0]["request_count"] == 2
    assert data[0]["request_size_sum"] == 0
    assert data[0]["response_size_sum"] > 0
    assert data[0]["response_times"][100] == 1
    assert data[0]["response_times"][220] == 1
    assert len(data[0]["request_sizes"]) == 0
    assert data[0]["response_sizes"][0] == 2
    assert data[1]["method"] == "POST"
    assert data[1]["request_size_sum"] > 0
    assert data[1]["response_size_sum"] == 0
    assert data[1]["request_sizes"][0] == 1


def test_validation_error_counter():
    from apitally.client.base import ValidationErrorCounter

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


def test_key_registry():
    from apitally.client.base import KeyRegistry

    keys = KeyRegistry()

    # Cannot get keys before they are initialized
    with pytest.raises(RuntimeError):
        keys.get("7ll40FB.DuHxzQQuGQU4xgvYvTpmnii7K365j9VI")

    keys.salt = "54fd2b80dbfeb87d924affbc91b77c76"
    keys.update(
        {
            "bcf46e16814691991c8ed756a7ca3f9cef5644d4f55cd5aaaa5ab4ab4f809208": {
                "key_id": 1,
                "api_key_id": 1,
                "name": "Test key 1",
                "scopes": ["test"],
                "expires_in_seconds": 60,
            },
            "ba05534cd4af03497416ef9db0a149a1234a4ded7d37a8bc3cde43f3ed56484a": {
                "key_id": 2,
                "api_key_id": 2,
                "name": "Test key 2",
                "expires_in_seconds": 0,
            },
        }
    )

    # Key with bcf46e16814691991c8ed756a7ca3f9cef5644d4f55cd5aaaa5ab4ab4f809208 is valid
    key = keys.get("7ll40FB.DuHxzQQuGQU4xgvYvTpmnii7K365j9VI")
    assert key is not None
    assert key.key_id == 1
    assert key.name == "Test key 1"
    assert key.expires_at is not None
    assert key.has_scopes(["test"])

    # Key with hash ba05534cd4af03497416ef9db0a149a1234a4ded7d37a8bc3cde43f3ed56484a is expired
    key = keys.get("We6Yr7Z.fzj8t8TuYcTB9uOnpc2P7l4qlysIlT8q")
    assert key is None

    # Key does not exist
    key = keys.get("F9vNgPM.fiXFjMxmSn1TZeuyIm0CxF7gfmfrjKSZ")
    assert key is None

    api_key_usage = keys.get_and_reset_usage_counts()
    assert api_key_usage == {1: 1}


def test_key_cache_base():
    from apitally.client.base import ApitallyKeyCacheBase

    class ApitallyKeyCache(ApitallyKeyCacheBase):
        def store(self, data: str) -> None:
            return

        def retrieve(self) -> str | None:
            return None

    key_cache = ApitallyKeyCache(client_id=CLIENT_ID, env=ENV)
    assert CLIENT_ID in key_cache.cache_key
    assert ENV in key_cache.cache_key
