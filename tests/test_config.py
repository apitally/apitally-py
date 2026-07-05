import logging
import os

import pytest

from apitally.shared import config


VALID_TOKEN = "apt_3kPmN9xQv2bR7tH4wZ8yL5cE"


def test_kwarg_beats_apitally_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APITALLY_ENV", "dev")
    cfg = config.configure(write_token=VALID_TOKEN, env="staging")
    assert cfg.env == "staging"


def test_write_token_from_env_var(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APITALLY_WRITE_TOKEN", VALID_TOKEN)
    cfg = config.configure()
    assert cfg.write_token == VALID_TOKEN
    assert not cfg.disabled


def test_disabled_via_env_var(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APITALLY_DISABLED", "1")
    cfg = config.configure(write_token=VALID_TOKEN)
    assert cfg.disabled


def test_invalid_token_logs_masked_form_only(caplog: pytest.LogCaptureFixture):
    invalid_token = "apt_3kPmN9xQv2bR7tH4wZ8yL5cEXTRA"
    with caplog.at_level(logging.ERROR, logger="apitally"):
        cfg = config.configure(write_token=invalid_token)
    assert cfg.disabled
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("apt_3kPm..." in m for m in messages)
    assert all(invalid_token not in m for m in messages)


def test_recall_semantics(caplog: pytest.LogCaptureFixture):
    first = config.configure(write_token=VALID_TOKEN, env="staging")
    with caplog.at_level(logging.WARNING, logger="apitally"):
        assert config.configure(write_token=VALID_TOKEN, env="staging") is first
        assert not caplog.records
        assert config.configure(write_token=VALID_TOKEN, env="dev") is first
        assert config.configure(write_token=VALID_TOKEN, env="prod") is first
    assert first.env == "staging"
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_semconv_helper(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OTEL_SEMCONV_STABILITY_OPT_IN", raising=False)
    config.ensure_semconv_opt_in()
    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http/dup"
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
    config.ensure_semconv_opt_in()
    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http"
