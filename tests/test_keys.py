import pytest


def test_keys():
    from starlette_apitally.keys import Keys

    keys = Keys()

    # Cannot get keys before they are initialized
    with pytest.raises(RuntimeError):
        keys.get("7ll40FB.DuHxzQQuGQU4xgvYvTpmnii7K365j9VI")

    keys.salt = "54fd2b80dbfeb87d924affbc91b77c76"
    keys.update(
        {
            "bcf46e16814691991c8ed756a7ca3f9cef5644d4f55cd5aaaa5ab4ab4f809208": {
                "key_id": 1,
                "name": "Test key 1",
                "scopes": ["test"],
                "expires_in_seconds": 60,
            },
            "ba05534cd4af03497416ef9db0a149a1234a4ded7d37a8bc3cde43f3ed56484a": {
                "key_id": 2,
                "name": "Test key 2",
                "expires_in_seconds": 0,
            },
        }
    )

    # Key with bcf46e16814691991c8ed756a7ca3f9cef5644d4f55cd5aaaa5ab4ab4f809208 is valid
    key = keys.get("7ll40FB.DuHxzQQuGQU4xgvYvTpmnii7K365j9VI")
    assert key.key_id == 1
    assert key.name == "Test key 1"
    assert key.expires_at is not None
    assert key.check_scopes(["test"])

    # Key with hash ba05534cd4af03497416ef9db0a149a1234a4ded7d37a8bc3cde43f3ed56484a is expired
    key = keys.get("We6Yr7Z.fzj8t8TuYcTB9uOnpc2P7l4qlysIlT8q")
    assert key is None

    # Key does not exist
    key = keys.get("F9vNgPM.fiXFjMxmSn1TZeuyIm0CxF7gfmfrjKSZ")
    assert key is None

    used_key_ids = keys.get_and_reset_used_key_ids()
    assert used_key_ids == [1]
