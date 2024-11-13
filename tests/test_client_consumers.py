def test_consumer_registry():
    from apitally.client.consumers import Consumer, ConsumerRegistry

    consumer_registry = ConsumerRegistry()
    consumer_registry.add_or_update_consumer(None)
    assert len(consumer_registry.consumers) == 0

    consumer_registry.add_or_update_consumer(Consumer("test"))
    assert len(consumer_registry.consumers) == 0

    consumer_registry.add_or_update_consumer(Consumer("test", name="Test"))
    consumer_registry.add_or_update_consumer(Consumer("test", group="Test"))
    assert len(consumer_registry.consumers) == 1
    assert consumer_registry.consumers["test"].name == "Test"
    assert consumer_registry.consumers["test"].group == "Test"

    consumer_registry.add_or_update_consumer(Consumer("test", group="Test 2"))
    assert len(consumer_registry.consumers) == 1
    assert consumer_registry.consumers["test"].name == "Test"
    assert consumer_registry.consumers["test"].group == "Test 2"

    consumer_registry.add_or_update_consumer(Consumer("test", name="Test 2"))
    assert len(consumer_registry.consumers) == 1
    assert consumer_registry.consumers["test"].name == "Test 2"
    assert consumer_registry.consumers["test"].group == "Test 2"

    assert len(consumer_registry.updated) == 1
    data = consumer_registry.get_and_reset_updated_consumers()
    assert len(data) == 1
    assert data[0]["identifier"] == "test"
    assert data[0]["name"] == "Test 2"
    assert data[0]["group"] == "Test 2"
    assert len(consumer_registry.updated) == 0
    assert len(consumer_registry.consumers) == 1
