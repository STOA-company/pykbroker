"""A′-1: mock (virtual) accounts are a first-class, verified mode — never inferred."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pykbroker.brokers.kis import KIS
from pykbroker.exceptions import ConfigurationError, DryRunError
from pykbroker.models import OrderRequest


def _client(virtual: bool):
    client, scope = Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=virtual)
    client.account.return_value = scope
    return client, scope


def _api():
    api = Mock()
    api.post_order_domestic.return_value = {"json_data": {"rt_cd": "0", "output": {"ODNO": "43"}}}
    api.get_open_orders_domestic.return_value = {"orders": [{"pdno": "005930", "odno": "42", "psbl_qty": "1"}]}
    return api


def test_virtual_flag_reaches_the_vendor_factory(live_safety):
    seen = {}

    def factory(**options):
        seen.update(options)
        client, _ = _client(True)
        return client

    broker = KIS(account="fixture", client_factory=factory, api=_api(), virtual=True, safety=live_safety)
    broker.get_balance.__self__  # noqa: B018 - broker is constructed
    broker.list_orders()
    assert seen["virtual_account"] is True


def test_real_account_is_the_default(live_safety):
    seen = {}

    def factory(**options):
        seen.update(options)
        client, _ = _client(False)
        return client

    broker = KIS(account="fixture", client_factory=factory, api=_api(), safety=live_safety)
    broker.list_orders()
    assert seen["virtual_account"] is False
    assert broker.capabilities().virtual is False
    assert broker.capabilities().supports_virtual_account is True


@pytest.mark.parametrize("requested,actual", [(False, True), (True, False)])
def test_injected_client_mode_must_match_the_request(live_safety, requested, actual):
    """Bypass 1: an injected client whose mode disagrees is never silently accepted."""
    client, _ = _client(actual)
    broker = KIS(account="fixture", client=client, api=_api(), virtual=requested, safety=live_safety)
    with pytest.raises(ConfigurationError, match="mode"):
        broker.list_orders()


@pytest.mark.parametrize("key", [None, SimpleNamespace(), SimpleNamespace(virtual_account="false"), Mock()])
def test_undisclosed_mode_is_refused_not_assumed(live_safety, key):
    """Bypass 2: an unreadable mode refuses execution instead of defaulting to real."""
    client = Mock()
    client.key = key
    broker = KIS(account="fixture", client=client, api=_api(), safety=live_safety)
    with pytest.raises(ConfigurationError, match="mode"):
        broker.list_orders()


def test_client_options_cannot_smuggle_the_mode(live_safety):
    """Bypass 3: the mode comes from the contract argument, not from vendor options."""
    with pytest.raises(ConfigurationError, match="virtual"):
        KIS(
            account="fixture",
            client_options={"appkey": "x", "appsecret": "y", "virtual_account": True},
            api=_api(),
            safety=live_safety,
        )


@pytest.mark.parametrize("value", ["true", 1, None, "False"])
def test_virtual_must_be_a_real_boolean(live_safety, value):
    """Bypass 4: truthy strings never promote or demote an account mode."""
    client, _ = _client(True)
    with pytest.raises(ConfigurationError, match="virtual"):
        KIS(account="fixture", client=client, api=_api(), virtual=value, safety=live_safety)


def test_submitted_order_records_the_mode_it_went_out_in(live_safety, request_order):
    client, scope = _client(True)
    api = _api()
    broker = KIS(account="fixture", client=client, api=api, virtual=True, safety=live_safety)
    order = broker.place_order(request_order)
    assert order.virtual is True and order.id == "43"
    assert broker.list_orders()[0].virtual is True
    assert broker.capabilities().virtual is True
    api.post_order_domestic.assert_called_once_with(
        kis_account=scope, stock_code="005930", order_type="buy", order_qty=1, order_price=70000, dvsn="지정가"
    )


def test_virtual_never_relaxes_the_safety_gate(request_order):
    client, _ = _client(True)
    broker = KIS(account="fixture", client=client, api=_api(), virtual=True)
    with pytest.raises(DryRunError):
        broker.place_order(request_order)


def test_real_order_is_not_labelled_virtual(live_safety, request_order):
    client, _ = _client(False)
    broker = KIS(account="fixture", client=client, api=_api(), safety=live_safety)
    assert broker.place_order(request_order).virtual is False


def test_us_virtual_is_supported_and_verified(live_safety):
    client, _ = _client(True)
    api = Mock()
    api.get_open_orders_overseas_all.return_value = {"json_data": {"rt_cd": "0", "output": []}}
    broker = KIS(account="fixture", market="US", venue="NASDAQ", client=client, api=api, virtual=True)
    assert broker.capabilities().virtual is True
    assert broker.list_orders() == []
    OrderRequest(symbol="TEST", side="buy", quantity=1, price="1", market="US", client_key="k")
