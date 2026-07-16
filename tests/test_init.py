import sys

import pytest

import apitally
from tests.conftest import WRITE_TOKEN, installed


@pytest.mark.skipif(not installed("litestar"), reason="requires litestar")
def test_init_rejects_litestar_app_pointing_to_plugin():
    from litestar import Litestar

    with pytest.raises(TypeError, match="ApitallyPlugin"):
        apitally.init(Litestar(route_handlers=[]), write_token=WRITE_TOKEN)


def test_init_rejects_unsupported_app_type():
    with pytest.raises(TypeError, match="could not detect"):
        apitally.init(object(), write_token=WRITE_TOKEN)


def test_init_without_app_outside_django_context_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delitem(sys.modules, "django.conf", raising=False)
    with pytest.raises(TypeError, match="requires an app argument"):
        apitally.init(write_token=WRITE_TOKEN)
