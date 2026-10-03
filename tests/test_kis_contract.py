from decimal import Decimal
from types import SimpleNamespace

import pytest

from pykbroker.base import BrokerAdapter, StockBroker
from pykbroker.exceptions import (
    BrokerRejectedError,
    DryRunError,
    DuplicateOrderError,
    InvalidResponseError,
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
    scope.daily_order_all.return_value = SimpleNamespace(orders=[])
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
    with pytest.raises(InvalidResponseError, match="branch identifier"):
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


def test_kis_exact_cancel_never_cancels_other_orders(kis):
    broker, _, api, scope = kis
    api.get_open_orders_domestic.return_value = {"orders": [
        {"odno": "41", "pdno": "005930", "psbl_qty": "7", "krx_fwdg_ord_orgno": "999"},
        {"odno": "42", "pdno": "005930", "psbl_qty": "2", "krx_fwdg_ord_orgno": "001"},
    ]}
    scope.cancel.return_value = SimpleNamespace(odno="43")
    result = broker.cancel_order(CancelRequest(order_id="42", symbol="005930", quantity=1, client_key="cancel"))
    assert result.status == "accepted" and result.id == "42"
    scope.cancel.assert_called_once()
    assert scope.cancel.call_args.kwargs["order"].odno == "42"
    assert scope.cancel.call_args.kwargs["order"].krx_fwdg_ord_orgno == "001"
    assert scope.cancel.call_args.kwargs["qty"] == 1
    api.cancel_orders_domestic.assert_not_called()


def test_kis_status_reads_full_fill_after_order_leaves_open_snapshot(kis):
    broker, _, _, scope = kis
    scope.daily_order_all.return_value = SimpleNamespace(orders=[
        {"odno": "42", "pdno": "005930", "ord_qty": "2", "tot_ccld_qty": "2",
         "rmn_qty": "0", "avg_prvs": "69000", "cncl_yn": "N"},
    ])
    result = broker.status(Order(id="42", symbol="005930"))
    assert result.status == "filled" and result.filled == 2 and result.fill_price == Decimal("69000")


def test_kis_partial_fill_zero_remaining_is_not_a_full_fill(kis):
    broker, _, _, scope = kis
    scope.daily_order_all.return_value = SimpleNamespace(orders=[
        {"odno": "42", "pdno": "005930", "ord_qty": "2", "tot_ccld_qty": "1",
         "rmn_qty": "0", "avg_prvs": "69000", "cncl_yn": "N"},
    ])
    assert broker.status(Order(id="42", symbol="005930")).status == "unknown"


@pytest.mark.parametrize("ack", [{}, {"ODNO": None}, {"ODNO": ""}])
def test_kis_missing_cancel_ack_never_reports_acceptance(kis, ack):
    broker, _, api, scope = kis
    api.get_open_orders_domestic.return_value = {"orders": [
        {"odno": "42", "pdno": "005930", "psbl_qty": "2", "ord_gno_brno": "001"},
    ]}
    scope.cancel.return_value = {"rt_cd": "0", "output": ack}
    with pytest.raises(InvalidResponseError, match="order identifier"):
        broker.cancel_order(CancelRequest(order_id="42", symbol="005930", quantity=1, client_key="cancel"))
