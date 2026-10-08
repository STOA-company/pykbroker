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


@pytest.mark.parametrize(
    "filled,remaining,quantity,status", [(1, 1, 2, "partial"), (2, 0, 2, "filled"), (1, 0, 2, "unknown")]
)
def test_dbfi_kr_execution_average_and_complete_quantity(dbfi, filled, remaining, quantity, status):
    broker, client, _ = dbfi
    client.get_transaction_history.return_value = {"rsp_cd": "00000", "Out1": [{
        "OrdNo": "42", "IsuNo": "A005930", "OrdQty": quantity,
        "AllExecQty": filled, "MrcAbleQty": remaining,
        "AvrExecPrc": "69900", "OrdPrc": "70000",
    }]}
    order = broker.list_orders()[0]
    assert order.fill_price == Decimal("69900")
    assert order.quantity == quantity and order.status == status


@pytest.mark.parametrize("average", [None, "0.00", ""])
def test_dbfi_missing_execution_price_never_uses_limit(dbfi, average):
    broker, client, _ = dbfi
    client.get_transaction_history.return_value = {"Out1": [{
        "OrdNo": "42", "IsuNo": "A005930", "AllExecQty": 1, "MrcAbleQty": 0,
        "OrdQty": 1, "AvrExecPrc": average, "OrdPrc": "70000",
    }]}
    assert broker.list_orders()[0].fill_price is None


def test_dbfi_balance_keeps_d2_deposit_and_available_cash_requeries_buying_power(dbfi):
    broker, client, _ = dbfi
    client.get_able_order_quantity.return_value = {
        "rsp_cd": "00000", "Out": {"MgnRat100pctOrdAbleAmt": "100000", "Dps": "900000"}}
    assert broker.get_balance().cash_available == Decimal("200000")
    client.get_able_order_quantity.assert_not_called()
    assert broker.available_cash(symbol="A005930", price=Decimal("70000")) == Decimal("100000")
    client.get_able_order_quantity.return_value["Out"]["MgnRat100pctOrdAbleAmt"] = "150000"
    assert broker.available_cash(symbol="005930", price=Decimal("70000")) == Decimal("150000")
    client.get_able_order_quantity.assert_called_with(
        region="domestic", stock_code="005930", order_type="2", price=70000)
    assert client.get_able_order_quantity.call_count == 2


@pytest.mark.parametrize("integrated,expected", [(False, "100"), (True, "300")])
def test_dbfi_us_cash_explicit_margin_selection(integrated, expected):
    from unittest.mock import Mock

    from pykbroker.brokers.dbfi import DBFI
    client = Mock()
    client.get_able_order_quantity.return_value = {"rsp_cd": "00000", "Out": {
        "AstkOrdAbleAmt": "100", "AstkOrdAbleAmt0": "300", "AstkOrdAbleAmt1": "900",
        "CsldtMgnUseYn": "Y"}}
    broker = DBFI(account="fixture", market="US", venue="NASDAQ", client=client, is_integrated=integrated)
    assert broker.available_cash(symbol="TEST", price=Decimal("120.50")) == Decimal(expected)
    client.get_able_order_quantity.assert_called_once_with(
        region="overseas", stock_code="TEST", order_type="2", price=120.5, won_fcurr_type="2")


@pytest.mark.parametrize("flag", ["N", None])
def test_dbfi_us_integrated_margin_requires_broker_confirmation(flag):
    from unittest.mock import Mock

    from pykbroker.brokers.dbfi import DBFI
    client = Mock()
    client.get_able_order_quantity.return_value = {"rsp_cd": "00000", "Out": {
        "AstkOrdAbleAmt0": "300", "CsldtMgnUseYn": flag}}
    broker = DBFI(account="fixture", market="US", venue="NASDAQ", client=client, is_integrated=True)
    with pytest.raises(InvalidResponseError, match="not confirmed"):
        broker.available_cash()


def test_dbfi_us_average_is_aggregate_execution_amount_in_usd():
    from unittest.mock import Mock

    from pykbroker.brokers.dbfi import DBFI
    client = Mock()
    row = {"OrdNo": "42", "SymCode": "TEST", "AstkOrdQty": 2,
           "AstkExecQty": 2, "AstkOrdRmqty": 0, "AstkExecAmt": "239", "CrcyCode": "USD",
           "AstkOrdPrc": "120.50"}
    client.get_transaction_history.return_value = {"rsp_cd": "00000", "Out": [row]}
    broker = DBFI(account="fixture", market="US", venue="NASDAQ", client=client)
    order = broker.list_orders()[0]
    assert order.fill_price == Decimal("119.5") and order.status == "filled"
    client.get_transaction_history.assert_called_once_with(
        region="overseas", execution_status="0", order_type="0", stock_code="", query_type="0", won_fcurr_type="2")
    row["CrcyCode"] = "KRW"
    with pytest.raises(InvalidResponseError, match="currency"):
        broker.list_orders()


@pytest.mark.parametrize("flag", ["true", 1, None])
def test_dbfi_integrated_flag_cannot_be_inferred(flag):
    from pykbroker.brokers.dbfi import DBFI
    from pykbroker.exceptions import ConfigurationError
    with pytest.raises(ConfigurationError, match="explicit boolean"):
        DBFI(account="fixture", is_integrated=flag)
