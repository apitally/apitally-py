import pytest

from apitally.shared import config


@pytest.fixture(autouse=True)
def reset_apitally_config():
    yield
    config.reset()
