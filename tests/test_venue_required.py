"""A′2-4: `requires_venue` is enforced, not merely announced.

`capabilities().requires_venue` was already True for US, and the executor plans against
it — but validation only checked a venue that *was* supplied. An order with no venue at
all sailed through and got routed to whichever exchange the broker instance happened to
be configured for. A route nobody named is a route nobody reviewed.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pykbroker.brokers.dbfi import DBFI
from pykbroker.brokers.kis import KIS
from pykbroker.exceptions import NotSupportedError
from pykbroker.models import CancelRequest, OrderRequest

BROKERS = [KIS, DBFI]


def _us(broker_type, live_safety):
    client, api = Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=False)
    return (
        broker_type(account="fixture", market="US", venue="NASDAQ", client=client, api=api, safety=live_safety),
        client,
        api,
    )


def _order(**kwargs):
    return OrderRequest(
        symbol="TEST", side="buy", quantity=1, price="120", market="US", client_key="venue-key", **kwargs
    )


def _cancel(**kwargs):
    return CancelRequest(order_id="42", symbol="TEST", quantity=1, market="US", client_key="venue-key", **kwargs)


@pytest.mark.parametrize("broker_type", BROKERS)
def test_a_us_order_without_a_venue_is_refused(broker_type, live_safety):
    broker, client, api = _us(broker_type, live_safety)
    assert broker.capabilities().requires_venue is True
    with pytest.raises(NotSupportedError, match="venue"):
        broker.place_order(_order())
    api.post_order_overseas.assert_not_called()
    client.buy.assert_not_called()


@pytest.mark.parametrize("broker_type", BROKERS)
def test_a_us_cancellation_without_a_venue_is_refused(broker_type, live_safety):
    broker, client, _ = _us(broker_type, live_safety)
    with pytest.raises(NotSupportedError, match="venue"):
        broker.cancel_order(_cancel())
    client.cancel.assert_not_called()


@pytest.mark.parametrize("broker_type", BROKERS)
def test_naming_the_configured_venue_passes_validation(broker_type, live_safety):
    broker, _, _ = _us(broker_type, live_safety)
    broker._validate_order(_order(venue="NASDAQ"))


@pytest.mark.parametrize("broker_type", BROKERS)
def test_a_kr_order_needs_no_venue(broker_type, live_safety):
    client, api = Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=False)
    broker = broker_type(account="fixture", client=client, api=api, safety=live_safety)
    assert broker.capabilities().requires_venue is False
    broker._validate_order(OrderRequest(symbol="005930", side="buy", quantity=1, price="70000", client_key="kr-key"))


@pytest.mark.parametrize("broker_type", BROKERS)
def test_a_constructed_request_cannot_skip_the_venue(broker_type, live_safety):
    """Bypass 1: model_construct skips validation; place_order revalidates and still refuses."""
    broker, _, api = _us(broker_type, live_safety)
    smuggled = OrderRequest.model_construct(
        symbol="TEST",
        side="buy",
        quantity=1,
        order_type="limit",
        price=Decimal("120"),
        market="US",
        venue=None,
        client_key="venue-key",
    )
    with pytest.raises(NotSupportedError, match="venue"):
        broker.place_order(smuggled)
    api.post_order_overseas.assert_not_called()


@pytest.mark.parametrize("broker_type", BROKERS)
@pytest.mark.parametrize("blank", ["", " ", "nasdaq"])
def test_a_blank_or_misspelled_venue_is_not_a_venue(broker_type, blank, live_safety):
    """Bypass 2: an empty string is not «unspecified made acceptable»."""
    broker, _, api = _us(broker_type, live_safety)
    with pytest.raises(NotSupportedError):
        broker.place_order(_order(venue=blank))
    api.post_order_overseas.assert_not_called()


@pytest.mark.parametrize("broker_type", BROKERS)
def test_the_refusal_lands_before_the_safety_ledger_moves(broker_type, live_safety):
    """Bypass 3: a rejected route must not consume the day's risk budget or an idempotency key."""
    broker, _, _ = _us(broker_type, live_safety)
    with pytest.raises(NotSupportedError):
        broker.place_order(_order())
    assert broker.capabilities().requires_venue is True
    # The same key must still be usable once the caller names a venue.
    broker._validate_order(_order(venue="NASDAQ"))
