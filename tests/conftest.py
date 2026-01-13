from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from asyncio import AbstractEventLoop
from pathlib import Path
from typing import Iterator

import pytest
from pytest_mock import MockerFixture


if os.getenv("PYTEST_RAISE", "0") != "0":

    @pytest.hookimpl(tryfirst=True)
    def pytest_exception_interact(call):
        raise call.excinfo.value

    @pytest.hookimpl(tryfirst=True)
    def pytest_internalerror(excinfo):
        raise excinfo.value


@pytest.fixture(scope="module")
def event_loop() -> Iterator[AbstractEventLoop]:
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session", autouse=True)
def mock_lock_dir(session_mocker: MockerFixture) -> Iterator[None]:
    temp_dir = tempfile.mkdtemp()
    session_mocker.patch("apitally.client.instance.LOCK_DIR", Path(temp_dir))
    yield
    shutil.rmtree(temp_dir, ignore_errors=True)
