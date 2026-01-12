from __future__ import annotations

from importlib.util import find_spec
from typing import TYPE_CHECKING, Iterator

import pytest
from pytest_httpx import HTTPXMock
from requests_mock import Mocker as RequestsMocker


if find_spec("opentelemetry.instrumentation") is None:
    pytest.skip("opentelemetry.instrumentation is not available", allow_module_level=True)

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture(scope="module")
def span_exporter() -> Iterator[InMemorySpanExporter]:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter


@pytest.fixture(autouse=True)
def clear_spans(span_exporter: InMemorySpanExporter) -> None:
    span_exporter.clear()


def test_instrument_sync_function(span_exporter: InMemorySpanExporter) -> None:
    from apitally.otel import instrument

    @instrument
    def my_sync_function(x: int) -> int:
        return x * 2

    result = my_sync_function(5)

    assert result == 10
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "my_sync_function"
    assert spans[0].attributes is not None
    assert spans[0].attributes["code.file.path"] == __file__
    assert (
        spans[0].attributes["code.function.name"]
        == "tests.test_otel.test_instrument_sync_function.<locals>.my_sync_function"
    )


async def test_instrument_async_function(span_exporter: InMemorySpanExporter) -> None:
    from apitally.otel import instrument

    @instrument
    async def my_async_function(x: int) -> int:
        return x * 2

    result = await my_async_function(5)

    assert result == 10
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "my_async_function"
    assert spans[0].attributes is not None
    assert spans[0].attributes["code.file.path"] == __file__
    assert (
        spans[0].attributes["code.function.name"]
        == "tests.test_otel.test_instrument_async_function.<locals>.my_async_function"
    )


def test_span_context_manager(span_exporter: InMemorySpanExporter) -> None:
    from apitally.otel import span

    with span("my_custom_span") as s:
        s.set_attribute("key", "value")

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "my_custom_span"
    assert spans[0].attributes is not None
    assert spans[0].attributes.get("key") == "value"


def test_instrument_httpx_global(span_exporter: InMemorySpanExporter, httpx_mock: HTTPXMock) -> None:
    import httpx

    from apitally.otel import instrument_httpx

    httpx_mock.add_response(url="https://example.com/test", json={"status": "ok"})

    instrument_httpx()

    with httpx.Client() as client:
        response = client.get("https://example.com/test")

    assert response.status_code == 200
    spans = span_exporter.get_finished_spans()
    assert any("example.com" in s.name or "GET" in s.name for s in spans)


def test_instrument_httpx_client(span_exporter: InMemorySpanExporter, httpx_mock: HTTPXMock) -> None:
    import httpx

    from apitally.otel import instrument_httpx

    httpx_mock.add_response(url="https://example.com/test", json={"status": "ok"})

    client = httpx.Client()
    instrument_httpx(client)

    response = client.get("https://example.com/test")
    client.close()

    assert response.status_code == 200
    spans = span_exporter.get_finished_spans()
    assert any("example.com" in s.name or "GET" in s.name for s in spans)


def test_instrument_requests(span_exporter: InMemorySpanExporter, requests_mock: RequestsMocker) -> None:
    import requests

    from apitally.otel import instrument_requests

    requests_mock.get("https://example.com/test", json={"status": "ok"})

    instrument_requests()

    response = requests.get("https://example.com/test")

    assert response.status_code == 200
    spans = span_exporter.get_finished_spans()
    assert any("example.com" in s.name or "GET" in s.name for s in spans)
