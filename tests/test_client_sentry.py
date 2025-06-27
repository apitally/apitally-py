import time
from importlib.util import find_spec

import pytest


if find_spec("sentry_sdk") is None:
    pytest.skip("sentry-sdk is not available", allow_module_level=True)


def test_get_sentry_event_id_async():
    import sentry_sdk
    from sentry_sdk.transport import Transport

    from apitally.client.sentry import get_sentry_event_id_async

    with pytest.raises(RuntimeError, match="not initialized"):
        get_sentry_event_id_async(lambda _: None, raise_on_error=True)

    class MockTransport(Transport):
        def __init__(self):
            super().__init__()
            self.events = []

        def capture_envelope(self, envelope):
            self.events.append(envelope)

    transport = MockTransport()
    sentry_sdk.init(
        dsn="https://1234567890@sentry.io/1234567890",
        transport=transport,
        auto_enabling_integrations=False,
    )

    event_id = None

    def callback(event_id_: str) -> None:
        nonlocal event_id
        event_id = event_id_

    sentry_sdk.capture_message("test")
    get_sentry_event_id_async(callback, raise_on_error=True)
    time.sleep(0.01)

    assert event_id is not None
    assert len(transport.events) == 1
    assert event_id == transport.events[0].items[0].payload.json["event_id"]
