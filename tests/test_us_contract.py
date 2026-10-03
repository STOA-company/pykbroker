from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pykbroker.brokers.dbfi import DBFI
from pykbroker.brokers.kis import KIS
from pykbroker.exceptions import ConfigurationError, InvalidResponseError, RiskCapError
from pykbroker.models import CancelRequest, OrderRequest


def test_kis_us_contract(live_safety):
    client, api, scope = Mock(), Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=False)
    client.account.return_value = scope
    api.get_price_overseas_detail.return_value = {"json_data": {"rt_cd": "0", "output": {"last": "120.50"}}}
    api.get_quote_overseas.return_value = {
        "json_data": {"rt_cd": "0", "output2": {"pask1": "120.60", "pbid1": "120.40"}}
    }
    api.get_balances.return_value = {
        "json_data": {
            "rt_cd": "0",
            "output1": [
                {
                    "pdno": "TEST",
                    "prdt_name": "Fixture",
                    "ccld_qty_smtl1": "2",
                    "ord_psbl_qty1": "1",
                    "avg_unpr3": "100",
                    "ovrs_now_pric1": "120.50",
                    "frcr_evlu_amt2": "241.00",
                }
            ],
        }
    }
    api.get_open_orders_overseas_all.return_value = {
        "json_data": {"rt_cd": "0", "output": [{"odno": "42", "pdno": "TEST"}]}
    }
    api.post_order_overseas.return_value = {"json_data": {"rt_cd": "0", "output": {"ODNO": "43"}}}
    broker = KIS(
        account="fixture",
        market="US",
        venue="NASDAQ",
        client=client,
        api=api,
        cash_provider=lambda scope: Decimal("1000"),
        safety=live_safety,
    )
    assert broker.get_quote("TEST", market="US").bid == Decimal("120.40")
    api.get_price_overseas_detail.assert_called_with(kis_account=scope, excd="NAS", code="TEST")
    assert broker.get_balance().currency == "USD"
    assert broker.get_balance().total == Decimal("1241")
    assert broker.get_positions()[0].quantity == 2
    assert broker.list_orders()[0].market == "US"
    order = OrderRequest(
        symbol="TEST", side="buy", quantity=1, price="120.50", market="US", venue="NASDAQ", client_key="us-key"
    )
    with pytest.raises(RiskCapError):
        broker.place_order(order)
    api.post_order_overseas.assert_not_called()
    live_safety.valuation = lambda request: Decimal("1000")
    assert broker.place_order(order).market == "US"
    api.post_order_overseas.assert_called_once_with(
        kis_account=scope, ticker="TEST", market="NASD", order_type="buy", order_qty=1, order_price=120.5
    )


def test_dbfi_us_contract(live_safety):
    client = Mock()
    client.get_stock_price.return_value = {"rsp_cd": "00000", "Out": {"Prpr": "120.50"}}
    client.get_order_book.return_value = {"rsp_cd": "00000", "Out": {"Askp1": "120.60", "Bidp1": "120.40"}}
    client.get_stock_balance.return_value = [
        {
            "rsp_cd": "00000",
            "Out2": [
                {
                    "SymCode": "TEST",
                    "AstkHanglIsuNm": "Fixture",
                    "AstkExecBaseQty": "2",
                    "AstkOrdAbleQty": "1",
                    "AstkAvrPchsPrc": "100",
                    "AstkNowPrc": "120.50",
                    "AstkEvalAmt": "241",
                }
            ],
        }
    ]
    client.get_transaction_history.return_value = {
        "rsp_cd": "00000",
        "Out": [
            {
                "OrdNo": "42",
                "SymCode": "TEST",
                "AstkExecQty": "1",
                "AstkOrdRmqty": "1",
            }
        ],
    }
    client.sell.return_value = {"rsp_cd": "00000", "Out": {"OrdNo": "43"}}
    client.cancel.return_value = {"rsp_cd": "00000"}
    broker = DBFI(
        account="fixture",
        market="US",
        venue="NASDAQ",
        client=client,
        cash_provider=lambda client: Decimal("1000"),
        safety=live_safety,
    )
    assert broker.get_quote("TEST", market="US").market == "US"
    client.get_stock_price.assert_called_with(region="overseas", stock_code="TEST", market_code="FN")
    assert broker.get_positions()[0].sellable == 1
    assert broker.get_balance().total == Decimal("1241")
    assert broker.list_orders()[0].status == "partial"
    live_safety.valuation = lambda request: Decimal("1000")
    broker.place_order(
        OrderRequest(
            symbol="TEST", side="sell", quantity=1, price="120.50", market="US", venue="NASDAQ", client_key="us-key"
        )
    )
    client.sell.assert_called_once_with(region="overseas", stock_code="TEST", quantity=1, price=120.5, price_type="1")
    broker.cancel_order(
        CancelRequest(order_id="42", symbol="TEST", quantity=1, market="US", venue="NASDAQ", client_key="cancel")
    )
    client.cancel.assert_called_once_with(region="overseas", order_no=42, stock_code="TEST", quantity=1)
    assert not broker.capabilities().supports_nxt


@pytest.mark.parametrize("broker_type", [KIS, DBFI])
def test_us_requires_explicit_venue(broker_type):
    client = Mock()
    client.key = SimpleNamespace(virtual_account=False)
    with pytest.raises(ConfigurationError, match="venue"):
        broker_type(account="fixture", market="US", client=client)


@pytest.mark.parametrize("broker_fixture", ["kis", "dbfi"])
def test_domestic_price_never_truncated(request, broker_fixture, request_order):
    broker = request.getfixturevalue(broker_fixture)[0]
    with pytest.raises(ConfigurationError, match="whole won"):
        broker.place_order(request_order.model_copy(update={"price": Decimal("70000.9")}))


def test_missing_dbfi_ack_is_not_success(dbfi, request_order):
    broker, client, _ = dbfi
    client.buy.return_value = {"Out": {"OrdNo": "42"}}
    with pytest.raises(InvalidResponseError, match="success status"):
        broker.place_order(request_order)


@pytest.mark.parametrize("integrated,key", [(False, "usd_gnrl_ord_psbl_amt"), (True, "usd_itgr_ord_psbl_amt")])
def test_kis_us_buying_power_is_broker_reported_normal_or_unified(integrated, key):
    client, api, scope = Mock(), Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=False)
    client.account.return_value = scope
    output = {"usd_gnrl_ord_psbl_amt": "20", "usd_itgr_ord_psbl_amt": "150",
              "usd_objt_amt": "99999", "usd_frst_bltn_exrt": "1300"}
    scope.client.request.return_value = SimpleNamespace(
        status_code=200, json=lambda: {"rt_cd": "0", "output": output}
    )
    scope.account.build_body.side_effect = lambda params: params
    api.get_balances.return_value = {"rt_cd": "0", "output1": []}
    broker = KIS(account="fixture", market="US", venue="NASDAQ", client=client,
                 api=api, is_integrated=integrated)
    assert broker.available_cash() == Decimal(output[key])
    assert scope.client.request.call_args.args == ("get", "/uapi/domestic-stock/v1/trading/intgr-margin")
    assert scope.client.request.call_args.kwargs["params"]["WCRC_FRCR_DVSN_CD"] == "01"
    output.pop(key)
    with pytest.raises(InvalidResponseError):
        broker.available_cash()


def test_kis_us_exact_cancel_and_ledger_full_fill(live_safety):
    from pykbroker.models import Order
    client, api, scope = Mock(), Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=False)
    client.account.return_value = scope
    live_safety.valuation = lambda request: Decimal("1000")
    broker = KIS(account="fixture", market="US", venue="NASDAQ", client=client,
                 api=api, safety=live_safety)
    api.get_open_orders_overseas_all.return_value = {"rt_cd": "0", "output": [
        {"odno": "42", "pdno": "TEST", "nccs_qty": "2", "ord_gno_brno": "001"},
    ]}
    scope.overseas_cancel.return_value = SimpleNamespace(odno="43")
    result = broker.cancel_order(CancelRequest(order_id="42", symbol="TEST", market="US",
                               venue="NASDAQ", quantity=1, client_key="cancel-us"))
    assert result.status == "accepted"
    args = scope.overseas_cancel.call_args.kwargs
    assert args["market"] == "NASD" and args["code"] == "TEST" and args["qty"] == 1
    assert args["order"].odno == "42"
    api.get_orders_overseas_all.return_value = {"rt_cd": "0", "output": [
        {"odno": "42", "pdno": "TEST", "ft_ord_qty": "2", "ft_ccld_qty": "2",
         "nccs_qty": "0", "ft_ccld_unpr3": "119.4", "rvse_cncl_dvsn": "00"},
    ]}
    result = broker.status(Order(id="42", symbol="TEST", market="US"))
    assert result.status == "filled" and result.fill_price is None
