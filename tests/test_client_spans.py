def test_span_collector_disabled():
    from apitally.client.spans import SpanCollector

    collector = SpanCollector(enabled=False)
    assert not collector.enabled
    assert collector.tracer is None

    with collector.collect() as trace_id:
        assert trace_id is None

    assert collector.included_span_ids == {}
    assert collector.collected_spans == {}

    spans = collector.get_and_clear_spans(0)
    assert spans == []


def test_span_collector_enabled():
    from opentelemetry import trace as trace_api

    from apitally.client.spans import SpanCollector

    collector = SpanCollector(enabled=True)
    assert collector.enabled
    assert collector.tracer is not None

    # Span created outside collect() should not be collected
    with collector.tracer.start_as_current_span("outside_span"):
        pass

    with collector.collect() as trace_id:
        assert trace_id is not None
        assert trace_id in collector.included_span_ids

        # Child span should be collected
        with collector.tracer.start_as_current_span("child_span", kind=trace_api.SpanKind.CLIENT) as span:
            span.set_attribute("key", "value")

    spans = collector.get_and_clear_spans(trace_id)
    assert spans is not None
    assert not any(s["name"] == "outside_span" for s in spans)
    assert len(spans) == 2
    assert {s["name"] for s in spans} == {"root", "child_span"}

    # Verify cleanup
    assert collector.included_span_ids == {}
    assert collector.collected_spans == {}
