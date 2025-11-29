def test_get_cpu_memory_usage():
    from apitally.client.resources import get_cpu_memory_usage

    # Ignore first interval
    get_cpu_memory_usage()

    data = get_cpu_memory_usage()
    assert data is not None
    assert data["cpu_percent"] >= 0
    assert data["memory_rss"] > 0
