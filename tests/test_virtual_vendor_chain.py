"""A′2-1: on a mock account, only the paths the vendor really serves are offered.

The first round tested this with `api=Mock()`, which answers anything and therefore
proved nothing: against the vendor, a mock account's open-order listing dies inside
`revisable_order`. These tests drive the vendor's own control flow (`tests/vendor_chain`)
instead, so the refusals are present and have to be handled rather than mocked away.
"""

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from pykbroker.brokers.kis import KIS
from pykbroker.exceptions import NotSupportedError
from pykbroker.models import CancelRequest

from .conftest import load_fixture
from .vendor_chain import (
    VIRTUAL_REVISABLE_REFUSAL,
    VendorClient,
    VendorDomestic,
    VendorOverseas,
    VendorRejected,
)

FIXTURES = ("amount", "balance", "book", "quote", "submit", "orders", "daily_orders")


def _fixtures():
    data = {name: load_fixture("kis", name) for name in FIXTURES}
    data["open_orders_overseas"] = load_fixture("kis", "open_orders_overseas")
    return data


def _kr(virtual: bool, **kwargs):
    data = _fixtures()
    client = VendorClient(virtual=virtual, fixtures=data)
    api = VendorDomestic(data)
    return KIS(account="fixture", client=client, api=api, virtual=virtual, **kwargs), client, api


def _us(virtual: bool, **kwargs):
    data = _fixtures()
    client = VendorClient(virtual=virtual, fixtures=data)
    api = VendorOverseas(data)
    broker = KIS(account="fixture", market="US", venue="NASDAQ", client=client, api=api, virtual=virtual, **kwargs)
    return broker, client, api


# --- the double is faithful ----------------------------------------------


def test_the_vendor_really_refuses_a_mock_open_order_listing():
    """Guard on the guard: if this ever stops raising, the tests below prove nothing."""
    _, client, api = _kr(virtual=True)
    with pytest.raises(NotImplementedError, match=VIRTUAL_REVISABLE_REFUSAL):
        api.get_open_orders_domestic(kis_account=client.scope)


def test_the_vendor_has_no_mock_route_for_the_us_open_order_tr():
    _, client, api = _us(virtual=True)
    with pytest.raises(VendorRejected):
        api.get_open_orders_overseas_all(kis_account=client.scope, market="NASD")


# --- KR mock: replaced with the TR the vendor does serve -----------------


def test_kr_mock_listing_uses_the_daily_ledger_tr_not_the_revisable_one():
    broker, client, _ = _kr(virtual=True)
    orders = broker.list_orders()
    assert client.scope.tr_ids == ["VTTC8001R"]
    assert [order.id for order in orders] == ["42"]
    order = orders[0]
    assert (order.quantity, order.filled, order.remaining) == (3, 1, 2)
    assert order.status == "partial" and order.virtual is True


def test_kr_real_listing_still_uses_the_revisable_order_tr():
    broker, client, _ = _kr(virtual=False)
    assert [order.id for order in broker.list_orders()] == ["42"]
    assert client.scope.tr_ids == ["TTTC0084R"]


def test_kr_mock_listing_filters_by_symbol_through_the_vendor_argument():
    broker, client, _ = _kr(virtual=True)
    assert broker.list_orders("000660") == []
    assert broker.list_orders("005930")[0].symbol == "005930"


def test_kr_mock_status_answers_from_the_ledger():
    broker, _, _ = _kr(virtual=True)
    open_order = broker.list_orders()[0]
    assert broker.status(open_order).remaining == 2


def test_kr_mock_admits_the_ledger_is_not_a_realtime_snapshot():
    """The mock TR is a daily ledger; the vendor warns today's rows lag. Say so."""
    assert _kr(virtual=True)[0].capabilities().realtime_open_orders is False
    assert _kr(virtual=False)[0].capabilities().realtime_open_orders is True
    assert _kr(virtual=True)[0].capabilities().supports_order_history is True


# --- US mock: declared unsupported and refused before the vendor is called


def test_us_mock_listing_is_declared_unsupported_and_refuses():
    broker, _, api = _us(virtual=True)
    assert broker.capabilities().supports_order_history is False
    with pytest.raises(NotSupportedError):
        broker.list_orders()
    assert api.calls == 0


def test_us_mock_status_refuses_rather_than_reporting_a_stale_order():
    """Bypass 1: status() must not launder an unsupported listing into 'unknown'."""
    broker, _, api = _us(virtual=True)
    order = broker.place_order  # noqa: B018 - broker is constructed
    with pytest.raises(NotSupportedError):
        broker.status(Mock(id="42", symbol="TEST"))
    assert api.calls == 0
    assert order is not None


def test_us_real_listing_still_works():
    broker, _, api = _us(virtual=False)
    assert [order.id for order in broker.list_orders()] == ["42"]
    assert api.calls == 1


# --- the declaration cannot be talked around -----------------------------


def test_a_capability_claim_is_never_wider_than_the_behaviour():
    """Bypass 2: whatever capabilities() claims, the call must actually do it."""
    for market, virtual in (("KR", True), ("KR", False), ("US", True), ("US", False)):
        broker = (_kr if market == "KR" else _us)(virtual)[0]
        if broker.capabilities().supports_order_history:
            broker.list_orders()
        else:
            with pytest.raises(NotSupportedError):
                broker.list_orders()


def test_single_order_cancel_is_not_claimed_on_either_kis_mode():
    """Bypass 3: KIS has no single-order cancel; a mock account does not invent one."""
    for virtual in (True, False):
        broker, client, _ = _kr(virtual)
        assert broker.capabilities().supports_single_order_cancel is False
        with pytest.raises(NotSupportedError):
            broker.cancel_order(CancelRequest(order_id="42", symbol="005930", quantity=1, client_key="cancel-key"))
        assert client.scope.tr_ids == []


def test_the_mock_ledger_query_asks_the_vendor_for_today_only():
    broker, client, _ = _kr(virtual=True)
    seen = {}
    original = client.scope.daily_order

    def spy(start_date, end_date, **kwargs):
        seen.update(start=start_date, end=end_date, **kwargs)
        return original(start_date, end_date, **kwargs)

    client.scope.daily_order = spy
    broker.list_orders()
    today = datetime.now(UTC).astimezone(seen["start"].tzinfo).date()
    assert seen["start"].date() == today and seen["end"].date() == today
    assert seen["ccld"] == "미체결"
