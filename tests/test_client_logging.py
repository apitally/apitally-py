import logging
from contextvars import ContextVar
from typing import Iterator, Optional

import pytest


@pytest.fixture
def log_buffer() -> Iterator[list[logging.LogRecord]]:
    from apitally.client.logging import LogHandler, setup_log_capture

    log_buffer_var: ContextVar[Optional[list[logging.LogRecord]]] = ContextVar("log_buffer", default=None)
    handler = LogHandler(log_buffer_var)
    buffer: list[logging.LogRecord] = []
    token = log_buffer_var.set(buffer)
    setup_log_capture(handler)

    yield buffer

    log_buffer_var.reset(token)
    logging.getLogger().removeHandler(handler)


def test_log_capture(log_buffer: list[logging.LogRecord]) -> None:
    test_logger = logging.getLogger("test")
    test_logger.setLevel(logging.INFO)
    test_logger.info("Standard message")

    assert len(log_buffer) == 1
    assert log_buffer[0].name == "test"
    assert log_buffer[0].levelno == logging.INFO
    assert log_buffer[0].levelname == "INFO"
    assert log_buffer[0].getMessage() == "Standard message"
    assert log_buffer[0].pathname == __file__


def test_log_capture_with_loguru(log_buffer: list[logging.LogRecord]) -> None:
    from loguru import logger

    logger.info("Loguru message")

    assert len(log_buffer) == 1
    assert log_buffer[0].name == "tests.test_client_logging"
    assert log_buffer[0].levelno == logging.INFO
    assert log_buffer[0].levelname == "INFO"
    assert log_buffer[0].getMessage() == "Loguru message"
    assert log_buffer[0].pathname == __file__
