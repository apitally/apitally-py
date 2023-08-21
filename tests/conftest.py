from __future__ import annotations

import asyncio
import os
from asyncio import AbstractEventLoop
from typing import TYPE_CHECKING, Iterator

import pytest


if TYPE_CHECKING:
    from apitally.client.base import KeyRegistry


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


@pytest.fixture(scope="module")
def key_registry() -> KeyRegistry:
    from apitally.client.base import KeyInfo, KeyRegistry

    key_registry = KeyRegistry()
    key_registry.salt = "54fd2b80dbfeb87d924affbc91b77c76"
    key_registry.keys = {
        "bcf46e16814691991c8ed756a7ca3f9cef5644d4f55cd5aaaa5ab4ab4f809208": KeyInfo(
            key_id=1,
            api_key_id=1,
            name="Test key",
            scopes=["foo"],
        )
    }
    return key_registry
