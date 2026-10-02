import threading
from typing import Any

from apitally.shared import server_errors
from apitally.shared.server_errors import ExceptionHolder


def test_server_errors_are_collapsed_aggregated_and_enriched() -> None:
    exception = ValueError("boom")
    exception_group_type: Any = server_errors.exception_group_types[0]
    holder = server_errors.init_exception_holder()
    server_errors.set_exception(exception_group_type("group", [exception]))

    server_errors.add_server_error("consumer", "get", "/items", holder)
    server_errors.set_sentry_event_id("event-id")
    server_errors.add_server_error("consumer", "GET", "/items", holder)
    server_errors.add_server_error(None, "GET", "/items", holder)

    (body,) = server_errors.drain_server_errors()
    assert body == {
        "method": "GET",
        "path": "/items",
        "type": "builtins.ValueError",
        "message": "boom",
        "stacktrace": "ValueError: boom",
        "counts": [{"consumer": "consumer", "count": 2}, {"count": 1}],
        "sentry_event_id": "event-id",
    }


def test_server_errors_from_different_call_sites_are_counted_separately() -> None:
    def raise_error(use_second_line: bool) -> None:
        if not use_second_line:
            raise RuntimeError("boom")
        raise RuntimeError("boom")

    for use_second_line in (False, True, False):
        try:
            raise_error(use_second_line)
        except RuntimeError as exception:
            server_errors.add_server_error(None, "GET", "/items", ExceptionHolder(exception))

    events = server_errors.drain_server_errors()
    assert sorted(event["counts"][0]["count"] for event in events) == [1, 2]
    assert len({event["stacktrace"] for event in events}) == 2


def test_server_error_requires_exception_route_and_non_options_method() -> None:
    holder = ExceptionHolder(RuntimeError("boom"))
    server_errors.add_server_error(None, "OPTIONS", "/items", holder)
    server_errors.add_server_error(None, "GET", None, holder)
    server_errors.add_server_error(None, "GET", "/items", ExceptionHolder())
    assert server_errors.drain_server_errors() == []


def test_exception_output_is_bounded() -> None:
    exception = RuntimeError("é" * (server_errors.MAX_EXCEPTION_MESSAGE_LENGTH + 1))
    message = server_errors.format_exception_message(exception)
    assert len(message) == server_errors.MAX_EXCEPTION_MESSAGE_LENGTH
    assert message.endswith(server_errors.MESSAGE_TRUNCATION_SUFFIX)
    assert server_errors.format_exception_type(exception) == "builtins.RuntimeError"

    try:
        try:
            raise ValueError("x" * server_errors.MAX_STACKTRACE_LENGTH)
        except ValueError as cause:
            raise RuntimeError("final") from cause
    except RuntimeError as chained:
        stacktrace = server_errors.format_exception_stacktrace(chained)
    assert len(stacktrace) <= server_errors.MAX_STACKTRACE_LENGTH
    assert stacktrace.startswith(server_errors.STACKTRACE_TRUNCATION_PREFIX.strip())
    assert stacktrace.endswith("RuntimeError: final")


def test_concurrent_server_error_add_and_drain_preserves_count() -> None:
    start = threading.Event()

    def add_errors() -> None:
        start.wait()
        for _ in range(1_000):
            server_errors.add_server_error(None, "GET", "/items", ExceptionHolder(RuntimeError("boom")))

    threads = [threading.Thread(target=add_errors) for _ in range(4)]
    for thread in threads:
        thread.start()
    start.set()
    count = 0
    while any(thread.is_alive() for thread in threads):
        count += sum(event["counts"][0]["count"] for event in server_errors.drain_server_errors())
    for thread in threads:
        thread.join()
    count += sum(event["counts"][0]["count"] for event in server_errors.drain_server_errors())
    assert count == 4_000


def test_distinct_server_errors_are_bounded() -> None:
    for index in range(server_errors.MAX_ERRORS + 1):
        server_errors.add_server_error(None, "GET", "/items", ExceptionHolder(RuntimeError(str(index))))
    consumers = [f"consumer-{index}" for index in range(server_errors.MAX_ERRORS + 1)]
    for consumer in consumers:
        server_errors.add_server_error(consumer, "GET", "/items", ExceptionHolder(RuntimeError("0")))

    events = server_errors.drain_server_errors()
    assert len(events) == server_errors.MAX_ERRORS
    assert next(event for event in events if event["message"] == "0")["counts"] == [
        {"count": 1},
        *({"consumer": consumer, "count": 1} for consumer in consumers),
    ]
