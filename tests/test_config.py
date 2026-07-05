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


def test_sample_rate_resolution():
    cfg = config.configure(write_token=VALID_TOKEN, sample_rate=0.3)
    assert cfg.sample_rate == 0.3
    config.reset()
    cfg = config.configure(write_token=VALID_TOKEN)
    assert cfg.sample_rate == 1.0
    config.reset()
    cfg = config.configure(write_token=VALID_TOKEN, sample_rate=0.0)
    assert cfg.sample_rate == 0.0


@pytest.mark.parametrize("invalid_rate", [1.5, -0.1, "0.5"])
def test_invalid_sample_rate_warns_once_and_captures_everything(invalid_rate: object, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING, logger="apitally"):
        cfg = config.configure(write_token=VALID_TOKEN, sample_rate=invalid_rate)
        config.configure(write_token=VALID_TOKEN, sample_rate=invalid_rate)
    assert cfg.sample_rate == 1.0
    warnings = [r for r in caplog.records if "sample_rate" in r.getMessage()]
    assert len(warnings) == 1


def test_sample_rate_adjustable_after_configure():
    cfg = config.configure(write_token=VALID_TOKEN, sample_rate=0.5)
    cfg.sample_rate = 0.2
    assert config.get_config() is cfg
    assert cfg.sample_rate == 0.2


def test_semconv_helper(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OTEL_SEMCONV_STABILITY_OPT_IN", raising=False)
    config.ensure_semconv_opt_in()
    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http/dup"
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
    config.ensure_semconv_opt_in()
    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http"
