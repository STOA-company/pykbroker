"""_TossBridge(base_url=) — None keeps the exact pre-change TossSettings call."""

import pytest

cfg = pytest.importorskip("toss_openapi.config")
# Import every SDK module the bridge loads before TossSettings is spied on (class bodies use it).
for _name in ("toss_openapi.broker.account_context", "toss_openapi.broker.order_guard", "toss_openapi.client",
              "toss_openapi.credentials", "toss_openapi.auth.token_manager", "toss_openapi.transport.http",
              "toss_openapi.models.order", "toss_openapi.errors"):
    pytest.importorskip(_name)

from pykbroker.brokers.toss import _TossBridge  # noqa: E402

CREDS = {"api_key": "fixture-client", "secret_key": "fixture-secret"}


@pytest.fixture
def settings_calls(monkeypatch):
    calls = []
    real = cfg.TossSettings

    def spy(**kw):
        calls.append(kw)
        return real(**kw)

    monkeypatch.setattr(cfg, "TossSettings", spy)
    return calls


def test_default_call_is_unchanged(settings_calls):
    _TossBridge(account_seq=1, credentials_options=CREDS, dry_run=False, live_trading_enabled=True)
    assert settings_calls == [{"dry_run": False, "live_trading_enabled": True}]


def test_base_url_reaches_settings(settings_calls):
    _TossBridge(account_seq=1, credentials_options=CREDS, base_url="http://127.0.0.1:18080")
    assert settings_calls == [{"base_url": "http://127.0.0.1:18080", "dry_run": True, "live_trading_enabled": False}]
