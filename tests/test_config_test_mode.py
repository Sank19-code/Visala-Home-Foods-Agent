# The demo must never be able to boot against real money.
import importlib

import pytest


def _load_config(monkeypatch, key_id):
    monkeypatch.setenv("RAZORPAY_KEY_ID", key_id)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "test_secret")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "test_webhook_secret")
    from src import config

    return importlib.reload(config)


def test_accepts_a_test_mode_key(monkeypatch):
    config = _load_config(monkeypatch, "rzp_test_abc123")
    assert config.settings.razorpay_key_id.startswith("rzp_test_")


def test_refuses_to_start_on_a_live_key(monkeypatch):
    with pytest.raises(RuntimeError, match="test mode only"):
        _load_config(monkeypatch, "rzp_live_abc123")


def test_spend_caps_have_safe_defaults(monkeypatch):
    config = _load_config(monkeypatch, "rzp_test_abc123")
    assert config.settings.max_order_amount_inr > 0
    assert config.settings.max_daily_amount_inr >= config.settings.max_order_amount_inr
    assert config.settings.require_user_confirmation is True
