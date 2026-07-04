import pytest
from opentelemetry.test.globals_test import reset_trace_globals

from apitally.shared import config, providers


@pytest.fixture(autouse=True)
def reset_apitally_config():
    yield
    config.reset()


@pytest.fixture(autouse=True)
def reset_otel_trace_globals():
    yield
    reset_trace_globals()
    providers.reset()
