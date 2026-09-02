import threading
from contextvars import copy_context
from typing import Any

from apitally.shared import server_errors
from apitally.shared.server_errors import ExceptionHolder


def test_mutable_exception_holder_is_shared_with_copied_context() -> None:
    holder = server_errors.init_exception_holder()
    exception = RuntimeError("boom")
    copy_context().run(server_errors.set_exception, exception)
    assert holder.exception is exception


def test_exception_replacement_collapse_and_sentry_id_rules() -> None:
    holder = server_errors.init_exception_holder()
    first = ValueError("first")
    server_errors.set_sentry_event_id(" a ")
    server_errors.set_exception(first)
    first_event_id = holder.sentry_event_id
    assert first_event_id == "a"

    exception_group_type: Any = server_errors.exception_group_types[0]
    server_errors.set_exception(exception_group_type("group", [first]))
    assert holder.exception is first
    assert holder.sentry_event_id == first_event_id

    second = RuntimeError("second")
    server_errors.set_exception(second)
    assert holder.exception is second
    assert holder.sentry_event_id is None
    server_errors.set_sentry_event_id("   ")
    assert holder.sentry_event_id is None
    server_errors.set_sentry_event_id("event-id")
    assert holder.sentry_event_id == "event-id"


def test_exception_formatting_preserves_v0_markers_and_trailing_traceback_lines() -> None:
    exception = RuntimeError("é" * (server_errors.MAX_EXCEPTION_MESSAGE_LENGTH + 1))
    message = server_errors.format_exception_message(exception)
    assert len(message) == server_errors.MAX_EXCEPTION_MESSAGE_LENGTH
    assert message.endswith(server_errors.MESSAGE_TRUNCATION_SUFFIX)

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
    assert server_errors.format_exception_type(exception) == "builtins.RuntimeError"


def test_server_error_eligibility_requires_exception_route_and_non_options_method() -> None:
    holder = ExceptionHolder(RuntimeError("boom"))
    server_errors.add_server_error(None, "OPTIONS", "/items", holder)
    server_errors.add_server_error(None, "GET", None, holder)
    server_errors.add_server_error(None, "GET", "/items", ExceptionHolder())
    assert server_errors.drain_server_errors() == []

    server_errors.add_server_error(None, "get", "/items", holder)
    (body,) = server_errors.drain_server_errors()
    assert body["method"] == "GET"
    assert body["path"] == "/items"
    assert body["type"] == "builtins.RuntimeError"
    assert body["message"] == "boom"
    assert body["count"] == 1
    assert "consumer" not in body
    assert "sentry_event_id" not in body


def test_equal_server_errors_sum_and_latest_non_empty_sentry_id_wins() -> None:
    exception = RuntimeError("boom")
    server_errors.add_server_error("consumer", "GET", "/items", ExceptionHolder(exception, "first"))
    server_errors.add_server_error("consumer", "GET", "/items", ExceptionHolder(exception))
    server_errors.add_server_error("consumer", "GET", "/items", ExceptionHolder(exception, "last"))

    (body,) = server_errors.drain_server_errors()
    assert body["consumer"] == "consumer"
    assert body["count"] == 3
    assert body["sentry_event_id"] == "last"


def test_each_server_error_identity_field_separates_groups() -> None:
    base = RuntimeError("boom")
    variants = [
        ("consumer", "GET", "/items", base),
        (None, "POST", "/items", base),
        (None, "GET", "/other", base),
        (None, "GET", "/items", ValueError("boom")),
        (None, "GET", "/items", RuntimeError("other")),
    ]
    for consumer, method, path, exception in variants:
        server_errors.add_server_error(consumer, method, path, ExceptionHolder(exception))

    try:
        raise RuntimeError("boom")
    except RuntimeError as exception_with_traceback:
        server_errors.add_server_error(None, "GET", "/items", ExceptionHolder(exception_with_traceback))
    assert len(server_errors.drain_server_errors()) == len(variants) + 1


def test_exception_character_limits_are_applied_before_server_error_grouping() -> None:
    first = RuntimeError("é" * server_errors.MAX_STACKTRACE_LENGTH + "a")
    second = RuntimeError("é" * server_errors.MAX_STACKTRACE_LENGTH + "b")
    server_errors.add_server_error(None, "GET", "/items", ExceptionHolder(first))
    server_errors.add_server_error(None, "GET", "/items", ExceptionHolder(second))

    events = server_errors.drain_server_errors()
    assert len(events) == 1
    assert events[0]["count"] == 2
    assert len(events[0]["message"]) == server_errors.MAX_EXCEPTION_MESSAGE_LENGTH
    assert len(events[0]["stacktrace"]) <= server_errors.MAX_STACKTRACE_LENGTH

    long_type = type("É" * 300, (Exception,), {})
    assert len(server_errors.format_exception_type(long_type())) == server_errors.MAX_EXCEPTION_TYPE_LENGTH


def test_server_error_identity_cap_keeps_incrementing_retained_groups() -> None:
    for index in range(server_errors.MAX_GROUPS + 1):
        server_errors.add_server_error(None, "GET", "/items", ExceptionHolder(RuntimeError(str(index))))
    server_errors.add_server_error(None, "GET", "/items", ExceptionHolder(RuntimeError("0")))

    events = server_errors.drain_server_errors()
    assert len(events) == server_errors.MAX_GROUPS
    assert next(event for event in events if event["message"] == "0")["count"] == 2
    assert all(event["message"] != str(server_errors.MAX_GROUPS) for event in events)


def test_concurrent_server_error_add_and_drain_preserves_occurrence_count() -> None:
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
        count += sum(event["count"] for event in server_errors.drain_server_errors())
    for thread in threads:
        thread.join()
    count += sum(event["count"] for event in server_errors.drain_server_errors())
    assert count == 4_000
