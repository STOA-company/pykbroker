"""Registered Kiwoom construction stays offline and uses the existing safety boundary."""

import json
import sys

import pytest

from pykbroker.base import BrokerAdapter
from pykbroker.brokers.kiwoom import KIWOOM_MOCK_BASE_URL, Kiwoom
from pykbroker.cli import main
from pykbroker.exceptions import ConfigurationError, SafetyError
from pykbroker.factory import BROKERS, create_broker
from pykbroker.models import CancelRequest, OrderRequest

OPTIONS = {"token": "fixture-token", "app_key": "fixture-app-key"}


def test_registered_constructor_uses_the_mock_host_without_issuing_a_token():
    made = []

    class FakeClient:
        def __init__(self, *, base_url):
            made.append(base_url)

        def post(self, *args, **kwargs):
            pytest.fail("construction must not send any request")

    broker = create_broker(
        "KIWOOM", account="fixture-account", client_options=OPTIONS, client_factory=FakeClient, virtual=True
    )
    assert BROKERS["kiwoom"] is Kiwoom and isinstance(broker, Kiwoom)
    assert isinstance(broker, BrokerAdapter)
    assert made == [KIWOOM_MOCK_BASE_URL]
    assert broker.capabilities().virtual is True
    broker.close()


def test_factory_reads_explicit_env_options_but_never_enables_orders(monkeypatch):
    monkeypatch.setenv("PYKBROKER_ACCOUNT", "fixture-account")
    monkeypatch.setenv("PYKBROKER_CLIENT_OPTIONS", json.dumps(OPTIONS))
    calls = []

    class FakeClient:
        def __init__(self, *, base_url):
            assert base_url == KIWOOM_MOCK_BASE_URL

        def post(self, *args, **kwargs):
            calls.append((args, kwargs))
            pytest.fail("default safety must block writes")

    with create_broker("kiwoom", client_factory=FakeClient, virtual=True) as broker:
        assert broker.safety.dry_run.enabled is True
        requests = (
            OrderRequest(symbol="005930", side="buy", quantity=1, price="70000", client_key="fixture-buy"),
            CancelRequest(symbol="005930", order_id="00024", quantity=1, client_key="fixture-cancel"),
        )
        for method, request in zip((broker.place_order, broker.cancel_order), requests, strict=True):
            with pytest.raises(SafetyError):
                method(request)
    assert calls == []


def test_factory_requires_stored_credentials_before_building_a_client(monkeypatch):
    monkeypatch.delenv("PYKBROKER_CLIENT_OPTIONS", raising=False)
    with pytest.raises(ConfigurationError, match="stored Kiwoom token"):
        create_broker("kiwoom", account="fixture-account")


def test_cli_lists_registered_kiwoom_without_credentials_or_calls(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pykbroker", "--broker", "kiwoom", "brokers"])
    main()
    assert "kiwoom" in json.loads(capsys.readouterr().out)
