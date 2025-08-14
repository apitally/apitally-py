import logging
import os
from contextvars import ContextVar
from logging import LogRecord
from typing import List, Optional


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

    def __init__(self, log_buffer_var: ContextVar[Optional[List[LogRecord]]]) -> None:
        super().__init__()
        self.log_buffer_var = log_buffer_var

    def emit(self, record: logging.LogRecord) -> None:
        buffer = self.log_buffer_var.get()
        if buffer is not None and len(buffer) < self.MAX_BUFFER_SIZE:
            buffer.append(record)
