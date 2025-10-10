def test_client_id_validation():
    from apitally.client.client_base import ApitallyClientBase

    ApitallyClientBase._instance = None
    client = ApitallyClientBase(client_id="xxx", env="test")
    assert client.enabled is False

    ApitallyClientBase._instance = None
    client = ApitallyClientBase(client_id="9003a5c6-0725-4502-8e57-963a21ba97b6", env="")
    assert client.enabled is False

    ApitallyClientBase._instance = None
    client = ApitallyClientBase(client_id="9003a5c6-0725-4502-8e57-963a21ba97b6", env="test")
    assert client.enabled is True

    ApitallyClientBase._instance = None
