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


@pytest.mark.parametrize("broker_code", ["dbfi", "nh", "toss"])
@pytest.mark.parametrize("provider_answer", [None, False, True])
def test_us_native_silence_cannot_be_cleared_by_quote_or_caller(broker_code, provider_answer):
    """Official-shaped quotes prove prices, not US permission; a caller may only veto.

    Provenance: docs/status/2026-10-04-pack-us-native-tradability.md.
    Payload values are synthetic and all transports are injected/offline.
    """
    from pykbroker.brokers.nh import NH
    from tests.test_toss_us_pilot import _broker

    provider = None if provider_answer is None else lambda symbol: provider_answer
    if broker_code == "dbfi":
        client = Mock()
        client.get_stock_price.return_value = {"rsp_cd": "00000", "Out": {"Prpr": "200"}}
        client.get_order_book.return_value = {"rsp_cd": "00000", "Out": {"Bidp1": "199", "Askp1": "201"}}
        broker = DBFI(account="fixture", market="US", venue="NASDAQ", client=client, halt_provider=provider)
    elif broker_code == "nh":
        class Client:
            def __init__(self, *, base_url):
                self.base_url = base_url

            def post(self, path, *, headers, data):
                assert path == "/gbstock/quote/v1/current"
                return 200, {"rsp_cd": "00000", "Output_0": {
                    "iem_cd": "AAPL", "trdprc": "200", "best_bid1": "199", "best_ask1": "201"}}, {}

        broker = NH(account="fixture", market="US", venue="US", client_factory=Client,
                    client_options={"app_key": "fixture-key", "token": "fixture-token", "acct_type": "01"},
                    halt_provider=provider)
    else:
        broker, made = _broker(halt_provider=provider)

    expected = True if provider_answer is True else None
    assert broker.get_rules("AAPL").halted is expected
    quote = broker.get_quote("AAPL", market="US")
    assert quote.last > 0 and quote.bid > 0 and quote.ask > 0
    rules = broker.rules("AAPL")
    assert rules.halted is expected
    # Isolate the instrument predicate from the separate session/calendar contract.
    assert rules.model_copy(update={"session_open": True}).tradable is False
    if broker_code == "dbfi":
        client.buy.assert_not_called()
        client.sell.assert_not_called()
    elif broker_code == "toss":
        assert all(call[0] not in {"submit_order", "cancel_order"} for call in made["sdk"].calls)


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

@pytest.mark.parametrize("native,expected", [("매매 가능", False), (None, None), ("", None),
    ("Y", None), ("N", None), ("1", None), ("0", None), (1, None), (0, None),
    (True, None), (False, None), ("매매 불가", None), (" 매매 가능 ", None)])
def test_kis_us_native_orderability_reuses_bound_quote_and_never_guesses_enum(native, expected):
    client, api, scope = Mock(), Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=False)
    client.account.return_value = scope
    api.get_price_overseas_detail.return_value = {"rt_cd": "0", "output": {
        "last": "100", "e_ordyn": native}}
    api.get_quote_overseas.return_value = {"rt_cd": "0", "output2": {"pbid1": "99", "pask1": "101"}}
    broker = KIS(account="fixture", market="US", venue="NASDAQ", client=client, api=api,
                 halt_provider=lambda symbol: False)
    broker.get_quote("AAPL", market="US")
    rules = broker.get_rules("AAPL")
    assert rules.halted is expected
    api.get_price_overseas_detail.assert_called_once_with(kis_account=scope, excd="NAS", code="AAPL")
    api.get_quote_overseas.assert_called_once_with(kis_account=scope, excd="NAS", code="AAPL")
    assert broker._us_native_orderability == {}


def test_kis_us_native_orderability_cannot_override_caller_halt_or_cross_venue_snapshot():
    client, api, scope = Mock(), Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=False)
    client.account.return_value = scope
    api.get_price_overseas_detail.return_value = {"rt_cd": "0", "output": {
        "last": "100", "e_ordyn": "매매 가능"}}
    api.get_quote_overseas.return_value = {"rt_cd": "0", "output2": {"pbid1": "99", "pask1": "101"}}
    broker = KIS(account="fixture", market="US", venue="NASDAQ", client=client, api=api,
                 halt_provider=lambda symbol: True)
    broker.get_quote("AAPL", market="US")
    assert broker.get_rules("AAPL").halted is True
    broker._halt_provider = None
    broker._us_native_orderability[("NASDAQ", "AAPL")] = True
    broker.venue = "NYSE"
    api.get_price_overseas_detail.return_value = {"rt_cd": "0", "output": {"last": "100"}}
    assert broker.get_rules("AAPL").halted is None
    api.get_price_overseas_detail.assert_called_with(kis_account=scope, excd="NYS", code="AAPL")
    # Neither a prior venue snapshot nor the earlier ticker may answer another query pair.
    assert broker.get_rules("MSFT").halted is None
    api.get_price_overseas_detail.assert_called_with(kis_account=scope, excd="NYS", code="MSFT")
