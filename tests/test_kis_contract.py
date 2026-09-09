from decimal import Decimal
from types import SimpleNamespace

import pytest

from pykbroker.base import BrokerAdapter, StockBroker
from pykbroker.exceptions import (
    BrokerRejectedError,
    DryRunError,
    DuplicateOrderError,
    InvalidResponseError,
    NotSupportedError,
    TransportError,
)
from pykbroker.models import CancelRequest, Order
from pykbroker.safety.gate import OrderSafety


def test_kis_recorded_contract(kis):
    broker, client, api, scope = kis
    assert isinstance(broker, StockBroker) and isinstance(broker, BrokerAdapter)
    assert broker.get_quote("005930").last == Decimal("70000")
    assert broker.get_quote("A005930").bid == Decimal("69900")
    client.account.assert_called_once_with("fixture-account")
    api.get_price_domestic.assert_called_with(kis_account=scope, stock_code="005930")
    assert broker.get_balance().total == Decimal("340000")
    assert broker.get_positions()[0].sellable == 1
    assert broker.list_orders()[0].id == "42"
    assert broker.list_orders("000660") == []
    assert broker.status(Order(id="absent", symbol="005930")).status == "unknown"


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_kis_exact_submission(kis, request_order, side):
    broker, _, api, scope = kis
    request = request_order.model_copy(update={"side": side})
    order = broker.place_order(request)
    assert order.id == "43" and order.status == "accepted" and order.filled is None
    api.post_order_domestic.assert_called_once_with(
        kis_account=scope, stock_code="005930", order_type=side, order_qty=1, order_price=70000, dvsn="지정가"
    )


def test_kis_sdk_object_response(kis, request_order):
    broker, _, api, _ = kis
    api.post_order_domestic.return_value = SimpleNamespace(odno="44")
    assert broker.place_order(request_order).id == "44"


def test_kis_dry_run_never_connects(kis, request_order):
    broker, client, api, _ = kis
    broker.safety = OrderSafety()
    with pytest.raises(DryRunError):
        broker.place(request_order)
    client.account.assert_not_called()
    api.post_order_domestic.assert_not_called()


def test_kis_never_broadens_cancel(kis):
    broker, _, api, _ = kis
    with pytest.raises(NotSupportedError):
        broker.cancel_order(CancelRequest(order_id="42", symbol="005930", quantity=1, client_key="cancel"))
    api.cancel_orders_domestic.assert_not_called()


@pytest.mark.parametrize("response", [{"json_data": {"rt_cd": "1"}}, {"json_data": {"rt_cd": "0", "output": {}}}])
def test_kis_response_failure_keeps_claim(kis, request_order, response):
    broker, _, api, _ = kis
    api.post_order_domestic.return_value = response
    with pytest.raises((BrokerRejectedError, InvalidResponseError)):
        broker.place_order(request_order)
    with pytest.raises(DuplicateOrderError):
        broker.place_order(request_order)
    assert api.post_order_domestic.call_count == 1


def test_kis_transport_is_sanitized_and_never_retried(kis, request_order):
    broker, _, api, _ = kis
    api.post_order_domestic.side_effect = OSError("sensitive payload must not leak")
    with pytest.raises(TransportError, match="reconcile") as error:
        broker.place_order(request_order)
    assert "sensitive" not in str(error.value)
    with pytest.raises(DuplicateOrderError):
        broker.place_order(request_order)
    assert api.post_order_domestic.call_count == 1


def test_kis_halted_book_is_unknown_not_zero(kis):
    broker, _, api, _ = kis
    api.get_quote_domestic.return_value = {"json_data": {"output1": {"askp1": "", "bidp1": "0"}}}
    quote = broker.get_quote("005930")
    assert quote.ask is None and quote.bid is None


def test_kis_missing_cash_is_an_error(kis):
    broker, _, _, scope = kis
    scope.amount.return_value = {}
    with pytest.raises(InvalidResponseError):
        broker.get_balance()


def test_borrowed_client_not_closed(kis):
    broker, client, _, _ = kis
    broker.close()
    broker.close()
    client.close.assert_not_called()
