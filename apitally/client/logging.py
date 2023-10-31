import logging
import os


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
