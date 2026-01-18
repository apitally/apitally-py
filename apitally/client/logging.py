from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from logging import LogRecord
from typing import TYPE_CHECKING, Optional


if TYPE_CHECKING:
    from loguru import Message as LoguruMessage


debug = os.getenv("APITALLY_DEBUG", "false").lower() in {"true", "yes", "y", "1"}
root_logger = logging.getLogger("apitally")

if debug:
    root_logger.setLevel(logging.DEBUG)
    console_handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class LogHandler(logging.Handler):
    MAX_BUFFER_SIZE = 1000

    def __init__(self, log_buffer_var: ContextVar[Optional[list[LogRecord]]]) -> None:
        super().__init__()
        self.log_buffer_var = log_buffer_var

    def emit(self, record: logging.LogRecord) -> None:
        buffer = self.log_buffer_var.get()
        if buffer is not None and len(buffer) < self.MAX_BUFFER_SIZE:
            buffer.append(record)


def setup_log_capture(handler: LogHandler) -> None:
    logging.getLogger().addHandler(handler)
    _try_setup_loguru_sink(handler)


def _try_setup_loguru_sink(handler: LogHandler) -> None:
    try:
        from loguru import logger

        def sink(message: LoguruMessage) -> None:
            record = message.record
            log_record = LogRecord(
                name=record["name"] or "",
                level=record["level"].no,
                pathname=record["file"].path,
                lineno=record["line"],
                msg=record["message"],
                args=(),
                exc_info=None,
            )
            log_record.created = record["time"].timestamp()
            log_record.levelname = record["level"].name
            handler.emit(log_record)

        logger.add(sink)
    except ImportError:  # pragma: no cover
        pass
