from decimal import Decimal

import pytest

from pykbroker.exceptions import BrokerRejectedError, DryRunError, DuplicateOrderError, InvalidResponseError
from pykbroker.models import CancelRequest
from pykbroker.safety.gate import OrderSafety


def test_dbfi_recorded_contract(dbfi):
    broker, client, _ = dbfi
    assert broker.get_quote("A005930").ask == Decimal("70100")
    client.get_stock_price.assert_called_with(region="domestic", stock_code="005930", market_code="J")
    assert broker.get_balance().cash_available == Decimal("200000")
    assert broker.get_positions()[0].quantity == 2
    orders = broker.list_orders("005930")
    assert len(orders) == 1 and orders[0].filled == 1 and orders[0].status == "partial"
    assert broker.list_orders("000660") == []
    assert broker.capabilities().supports_single_order_cancel


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("venue", ["KRX", "NXT"])
def test_dbfi_exact_order_mapping(dbfi, request_order, side, venue):
    broker, client, _ = dbfi
    order = broker.place_order(request_order.model_copy(update={"side": side, "venue": venue}))
    assert order.id == "43"
    getattr(client, side).assert_called_once_with(
        region="domestic", stock_code="005930", quantity=1, price=70000, price_type="00", use_nxt=venue == "NXT"
    )
    getattr(client, "buy" if side == "sell" else "sell").assert_not_called()


def test_dbfi_cancel_exact_quantity_no_retry(dbfi):
    broker, client, _ = dbfi
    client.cancel.return_value = {"rsp_cd": "99999"}
    request = CancelRequest(order_id="42", symbol="005930", quantity=2, client_key="cancel-once")
    with pytest.raises(BrokerRejectedError):
        broker.cancel_order(request)
    with pytest.raises(DuplicateOrderError):
        broker.cancel_order(request)
    client.cancel.assert_called_once_with(
        region="domestic", order_no=42, stock_code="005930", quantity=2, use_nxt=False
    )


def test_dbfi_cancel_ack_does_not_invent_final_state(dbfi):
    broker, _, _ = dbfi
    result = broker.cancel_order(CancelRequest(order_id="42", symbol="005930", quantity=1, client_key="cancel"))
    assert result.status == "accepted" and result.filled is None


def test_dbfi_dry_run_blocks_place_and_cancel(dbfi, request_order):
    broker, client, _ = dbfi
    broker.safety = OrderSafety()
    with pytest.raises(DryRunError):
        broker.place_order(request_order)
    with pytest.raises(DryRunError):
        broker.cancel_order(CancelRequest(order_id="42", symbol="005930", quantity=1, client_key="cancel"))
    client.buy.assert_not_called()
    client.cancel.assert_not_called()


@pytest.mark.parametrize("response", [{}, [{"Out1": []}, {}], {"rsp_cd": "failed", "Out1": []}])
def test_dbfi_malformed_pages_fail(dbfi, response):
    broker, client, _ = dbfi
    client.get_transaction_history.return_value = response
    with pytest.raises((InvalidResponseError, BrokerRejectedError)):
        broker.list_orders()


def test_dbfi_zero_remaining_not_a_fill_claim(dbfi):
    broker, client, _ = dbfi
    client.get_transaction_history.return_value = {
        "Out1": [{"OrdNo": "42", "IsuNo": "A005930", "AllExecQty": "0", "MrcAbleQty": "0"}]
    }
    assert broker.list_orders()[0].status == "unknown"
