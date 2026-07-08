import os

import pytest

from apitally.shared import config


VALID_TOKEN = "apt_3kPmN9xQv2bR7tH4wZ8yL5cE"


def test_kwarg_beats_apitally_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APITALLY_ENV", "dev")
    cfg = config.set_config(write_token=VALID_TOKEN, env="staging")
    assert cfg.env == "staging"


def test_env_from_env_var(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APITALLY_ENV", "dev")
    cfg = config.set_config(write_token=VALID_TOKEN)
    assert cfg.env == "dev"


def test_write_token_from_env_var(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APITALLY_WRITE_TOKEN", VALID_TOKEN)
    cfg = config.set_config()
    assert cfg.write_token == VALID_TOKEN
    assert not cfg.disabled


def test_disabled_via_env_var(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APITALLY_DISABLED", "1")
    cfg = config.set_config(write_token=VALID_TOKEN)
    assert cfg.disabled


def test_invalid_token_disables_config():
    invalid_token = "apt_3kPmN9xQv2bR7tH4wZ8yL5cEXTRA"
    cfg = config.set_config(write_token=invalid_token)
    assert cfg.disabled


def test_recall_semantics():
    first = config.set_config(write_token=VALID_TOKEN, env="staging")
    assert config.set_config(write_token=VALID_TOKEN, env="staging") is first
    assert config.set_config(write_token=VALID_TOKEN, env="dev") is first
    assert config.set_config(write_token=VALID_TOKEN, env="prod") is first
    assert first.env == "staging"


def test_sample_rate_resolution():
    cfg = config.set_config(write_token=VALID_TOKEN, sample_rate=0.3)
    assert cfg.sample_rate == 0.3
    config.reset()
    cfg = config.set_config(write_token=VALID_TOKEN)
    assert cfg.sample_rate == 1.0
    config.reset()
    cfg = config.set_config(write_token=VALID_TOKEN, sample_rate=0.0)
    assert cfg.sample_rate == 0.0


@pytest.mark.parametrize("invalid_rate", [1.5, -0.1, "0.5"])
def test_invalid_sample_rate_falls_back_to_default(invalid_rate: object):
    cfg = config.set_config(write_token=VALID_TOKEN, sample_rate=invalid_rate)
    assert cfg.sample_rate == 1.0


def test_semconv_helper(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OTEL_SEMCONV_STABILITY_OPT_IN", raising=False)
    config.ensure_semconv_opt_in()
    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http/dup"
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
    config.ensure_semconv_opt_in()
    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http"
