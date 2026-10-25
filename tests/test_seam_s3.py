"""S-추가3 offline walks: request fields checked against pinned official sources."""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from pykbroker.brokers.kis import KIS
from pykbroker.brokers.meritz import Meritz
from pykbroker.exceptions import ConfigurationError, DuplicateOrderError, InvalidResponseError, NotSupportedError
from pykbroker.models import AmendRequest, CancelRequest, OrderRequest
from pykbroker.safety.rate_limit import RateLimiter

from .meritz_contract_server import ContractServer
from .test_kiwoom_order_contract import make


@pytest.fixture
def safety(live_safety, monkeypatch):
    monkeypatch.setattr(RateLimiter, "wait", lambda *args: None)
    live_safety.valuation = lambda request: Decimal(1000)
    live_safety.market_order_allowed = lambda request: True
    return live_safety


def req(market, venue, **kw):
    return OrderRequest(**({"symbol": "0700", "market": market, "venue": venue, "side": "buy",
                           "quantity": 1, "price": "100", "client_key": "s3"} | kw))


MERITZ_MARKETS = [("US", "NASDAQ", "0537", "USD"), ("HK", "HKEX", "0104", "HKD"),
                 ("CN_SH", "SSE", "0215", "CNY"), ("CN_SZ", "SZSE", "0214", "CNY"),
                 ("JP_TSE", "TSE", "0106", "JPY"), ("JP_OSE", "OSE", "0107", "JPY"),
                 ("JP_JASDAQ", "JASDAQ", "0112", "JPY"), ("VN_HOSE", "HOSE", "0770", "VND"),
                 ("VN_HNX", "HNX", "0771", "VND")]


def meritz(safety, market, venue):
    server = ContractServer()
    broker = Meritz(account="fixture-account", bound_account="fixture-account", app_key="fixture-key",
                    token="fixture-token", mac_address="001122AABBCC", client=server, market=market,
                    venue=venue, order_date=date(2026, 10, 25), writes_enabled=True, safety=safety)
    return server, broker


@pytest.mark.parametrize("market,venue,vendor,currency", MERITZ_MARKETS)
@pytest.mark.parametrize("kind,price,native,condition", [("limit", "100", "01", "0"), ("market", None, "05", "0"),
    ("loo", "100", "31", "2"), ("loc", "100", "32", "7"), ("moo", None, "33", "2"), ("moc", None, "34", "7")])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_meritz_overseas_fields_and_open_order(
        safety, market, venue, vendor, currency, kind, price, native, condition, side):
    server, broker = meritz(safety, market, venue)
    request = req(market, venue, order_type=kind, price=price, side=side)
    placed = broker.place_order(request)
    body = server.calls[-1][3]
    assert body["ovrs_exch_vndr_idno"] == vendor
    assert (body["oder_cls_code"], body["oder_cond_cls_code"], body["ovrs_stck_oder_prc"]) == (
        native, condition, price or "0")
    assert placed.market == market and placed.status == "accepted"
    [row] = broker.list_orders()
    assert (row.market, row.side, row.order_type, row.remaining) == (market, side, kind, 1)
    assert broker.capabilities().currency == {market: currency}
    with pytest.raises(DuplicateOrderError):
        broker.place_order(request)
    assert len([call for call in server.calls if call[0] == "POST"]) == 1
    broker.cancel_order(CancelRequest(order_id=placed.id, symbol=placed.symbol, market=market,
                                     venue=venue, quantity=1, client_key="s3-cancel"))
    assert server.calls[-1][3]["oder_cls_code"] == native
    assert broker.status(placed).status == "cancelled"


@pytest.mark.parametrize("kind", ["vwap", "twap", "conditional_limit"])
def test_undeclared_kind_never_sends(safety, kind):
    server, broker = meritz(safety, "US", "NASDAQ")
    kw = {"algo_start": "09:30", "algo_end": "15:30"} if kind in {"vwap", "twap"} else {}
    with pytest.raises(NotSupportedError):
        broker.place_order(req("US", "NASDAQ", order_type=kind, **kw))
    assert server.calls == []


@pytest.mark.parametrize("market", ["HK", "CN_SH", "JP_TSE", "VN_HNX"])
def test_undeclared_market_never_constructs_transport(market):
    with pytest.raises(NotSupportedError):
        make(market=market)


@pytest.mark.parametrize("kw", [{"order_type": "limit", "algo_start": "09:30", "algo_end": "10:30"},
    {"order_type": "vwap"}, {"order_type": "twap", "algo_start": "10:30", "algo_end": "09:30"}])
def test_algorithm_period_is_not_silently_dropped(kw):
    with pytest.raises(ValidationError):
        req("US", "NASDAQ", **kw)


def test_algorithm_period_model_and_cancel_market():
    request = req("HK", "HKEX", order_type="vwap", algo_start="09:30", algo_end="15:30")
    assert request.algo_start.hour == 9 and request.algo_end.hour == 15
    assert CancelRequest(order_id="1", symbol="0700", market="HK", quantity=1, client_key="c").market == "HK"


KIS_MARKETS = [("HK", "HKEX", "SEHK", "TTTS1002U", "HKD"), ("CN_SH", "SSE", "SHAA", "TTTS0202U", "CNY"),
               ("CN_SZ", "SZSE", "SZAA", "TTTS0305U", "CNY"), ("JP_TSE", "TSE", "TKSE", "TTTS0308U", "JPY"),
               ("VN_HOSE", "HOSE", "VNSE", "TTTS0311U", "VND"), ("VN_HNX", "HNX", "HASE", "TTTS0311U", "VND")]


def kis_broker(safety, market, venue, virtual=False):
    scope = Mock()
    scope.account.build_body.side_effect = lambda body: {"CANO": "00000000", "ACNT_PRDT_CD": "01", **body}
    scope.client.request.return_value = {"rt_cd": "0", "output": {"ODNO": "101"}}
    client = SimpleNamespace(key=SimpleNamespace(virtual_account=virtual), account=lambda account: scope)
    return KIS(account="fixture-account", market=market, venue=venue, client=client, api=Mock(),
               safety=safety, virtual=virtual), scope


@pytest.mark.parametrize("market,venue,exchange,tr,currency", KIS_MARKETS)
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_kis_market_wire_code(safety, market, venue, exchange, tr, currency, side):
    broker, scope = kis_broker(safety, market, venue)
    placed = broker.place_order(req(market, venue, side=side))
    call = scope.client.request.call_args
    assert call.args == ("post", "/uapi/overseas-stock/v1/trading/order")
    assert call.kwargs["body"]["OVRS_EXCG_CD"] == exchange
    sell_tr = {"HK": "TTTS1001U", "CN_SH": "TTTS1005U", "CN_SZ": "TTTS0304U", "JP_TSE": "TTTS0307U",
               "VN_HOSE": "TTTS0310U", "VN_HNX": "TTTS0310U"}
    assert call.kwargs["headers"]["tr_id"] == (tr if side == "buy" else sell_tr[market])
    assert call.kwargs["body"]["ORD_QTY"] == "1" and call.kwargs["body"]["OVRS_ORD_UNPR"] == "100"
    assert placed.market == market and broker.capabilities().currency[market] == currency
    assert ("ORD_DVSN" in call.kwargs["body"]) is (market == "HK" and side == "sell")


@pytest.mark.parametrize("kind,side,price,native", [("loo", "buy", "100", "32"), ("loc", "buy", "100", "34"),
    ("moo", "sell", None, "31"), ("moc", "sell", None, "33")])
def test_kis_us_special_fields(safety, kind, side, price, native):
    broker, scope = kis_broker(safety, "US", "NASDAQ")
    broker.place_order(req("US", "NASDAQ", order_type=kind, side=side, price=price))
    assert scope.client.request.call_args.kwargs["body"]["ORD_DVSN"] == native


@pytest.mark.parametrize("virtual,kind,side", [(True, "loc", "buy"), (False, "moo", "buy"), (False, "moc", "buy")])
def test_kis_unsupported_side_or_mock_never_sends(safety, virtual, kind, side):
    broker, scope = kis_broker(safety, "US", "NASDAQ", virtual=virtual)
    with pytest.raises(NotSupportedError):
        broker.place_order(req("US", "NASDAQ", order_type=kind, side=side, price="100" if kind == "loc" else None))
    scope.client.request.assert_not_called()


@pytest.mark.parametrize("kind,side,price,native", [("limit", "buy", "100", "00"), ("loc", "buy", "100", "30"),
    ("market", "buy", None, "03"), ("moc", "sell", None, "33"), ("stop_limit", "sell", "100", "34")])
def test_kiwoom_us_fields(safety, kind, side, price, native):
    tr = "ust20000" if side == "buy" else "ust20001"
    broker, client = make({tr: [{"return_code": 0, "ord_no": "123456789"}]},
                          safety=safety, market="US", venue="NASDAQ")
    broker.place_order(req("US", "NASDAQ", order_type=kind, side=side, price=price,
                           **({"stop_price": "99"} if kind == "stop_limit" else {})))
    body = client.calls[-1]["json"]
    assert (body["stex_tp"], body["trde_tp"], body["ord_uv"]) == ("ND", native, price or "")
    source = json.loads((Path(__file__).parent / "fixtures/kiwoom/official/s3-orders.json").read_text())
    fields = next(v["request"]["body"] for k, v in source.items() if tr in k)
    assert set(body) <= {f["element"] for f in fields}
    assert native in next(f["description"] for f in fields if f["element"] == "trde_tp")
    if kind == "stop_limit":
        assert body["stop_pric"] == "99"


@pytest.mark.parametrize("kind,native,condition,price", [("best_limit", "12", "0", None),
    ("priority_limit", "13", "0", None), ("mid", "84", "0", None),
    ("ioc", "01", "3", "100"), ("fok", "01", "4", "100")])
def test_compatibility_kinds_use_existing_kr_transport(safety, kind, native, condition, price):
    server, broker = meritz(safety, "KR", "KRX")
    broker.place_order(req("KR", "KRX", symbol="005930", order_type=kind, price=price))
    assert (server.calls[-1][3]["oder_cls_code"], server.calls[-1][3]["oder_cond_cls_code"]) == (native, condition)


@pytest.mark.parametrize("market,venue,exchange,tr,currency", KIS_MARKETS)
def test_kis_new_market_ledger_and_cash_never_use_usd_or_nasdaq(safety, market, venue, exchange, tr, currency):
    broker, scope = kis_broker(safety, market, venue)
    placed = broker.place_order(req(market, venue))
    scope.client.request.return_value = {"rt_cd": "0", "output": [{
        "odno": placed.id, "pdno": "0700", "ft_ord_qty": "1", "ft_ccld_qty": "1", "nccs_qty": "0",
        "rvse_cncl_dvsn": "00", "ft_ccld_unpr3": "100"}]}
    state = broker.status(placed)
    assert state.status == "filled" and state.market == market and state.filled == 1
    assert scope.client.request.call_args.kwargs["params"]["OVRS_EXCG_CD"] == exchange
    assert scope.client.request.call_args.kwargs["params"]["ODNO"] == ""
    broker._api.get_balances.return_value = {"rt_cd": "0", "output1": [], "output2": [
        {"crcy_cd": "USD", "frcr_dncl_amt_2": "9999", "frcr_drwg_psbl_amt_1": "9999"},
        {"crcy_cd": currency, "frcr_dncl_amt_2": "200", "frcr_drwg_psbl_amt_1": "150"}]}
    balance = broker.get_balance()
    assert (balance.currency, balance.cash_available, balance.position_value) == (currency, 150, 0)
    assert broker.available_cash() == 150
    expected = {"HK": ("344", "01"), "CN_SH": ("156", "03"), "CN_SZ": ("156", "04"),
                "JP_TSE": ("392", "01"), "VN_HOSE": ("704", "02"), "VN_HNX": ("704", "01")}
    broker.get_positions()
    for call in broker._api.get_balances.call_args_list:
        assert (call.kwargs["NATN_CD"], call.kwargs["TR_MKET_CD"]) == expected[market]


@pytest.mark.parametrize("market,venue,vendor,currency", MERITZ_MARKETS[1:])
def test_meritz_cash_uses_market_currency(safety, market, venue, vendor, currency):
    server, broker = meritz(safety, market, venue)
    server.overrides["/trading/v1/overseas/orders/estimate"] = {
        "rsp_cd": "5766", "tr_cont": "0", "data": [
            {"crcd": "USD", "oder_crnc_oder_able_camt": "9999"},
            {"crcd": currency, "oder_crnc_oder_able_camt": "150"}]}
    assert broker.available_cash() == 150
    assert broker.get_balance().currency == currency


def test_new_kis_order_timeout_stays_unknown_and_cannot_resend(safety):
    from pykbroker.exceptions import TransportError
    broker, scope = kis_broker(safety, "HK", "HKEX")
    scope.client.request.side_effect = TimeoutError("fixture error")
    request = req("HK", "HKEX")
    with pytest.raises(TransportError):
        broker.place_order(request)
    with pytest.raises(DuplicateOrderError):
        broker.place_order(request)
    assert scope.client.request.call_count == 1


def test_meritz_opaque_suffix_requires_actual_today_venue_before_cancel(safety):
    server, broker = meritz(safety, "JP_OSE", "OSE")
    placed = broker.place_order(req("JP_OSE", "OSE"))
    server.today[placed.id]["ovrs_exch_vndr_idno"] = "0106"
    from pykbroker.exceptions import InvalidResponseError
    with pytest.raises(InvalidResponseError):
        broker.cancel_order(CancelRequest(order_id=placed.id, symbol=placed.symbol, market="JP_OSE",
                                         venue="OSE", quantity=1, client_key="cancel"))
    assert len([call for call in server.calls if call[0] == "POST"]) == 1


@pytest.mark.parametrize("market,venue,vendor,currency", MERITZ_MARKETS)
@pytest.mark.parametrize("kind,native,condition", [("limit", "01", "0"), ("loo", "31", "2"), ("loc", "32", "7")])
def test_meritz_amend_preserves_original_kind(safety, market, venue, vendor, currency, kind, native, condition):
    server, broker = meritz(safety, market, venue)
    placed = broker.place_order(req(market, venue, order_type=kind))
    server.calls.clear()
    broker.amend_order(AmendRequest(order_id=placed.id, symbol=placed.symbol, market=market, venue=venue,
                                   side="buy", quantity=1, price="101", client_key="amend"))
    writes = [c for c in server.calls if c[0] == "POST"]
    assert len(writes) == 1 and writes[0][1].endswith("/modify")
    body = writes[0][3]
    assert (body["oder_cls_code"], body["oder_cond_cls_code"], body["ovrs_stck_oder_prc"]) == (native, condition, "101")
    assert any(c[1].endswith("/today") for c in server.calls)


@pytest.mark.parametrize("kind", ["market", "moo", "moc"])
def test_meritz_unpriced_original_amend_never_sends(safety, kind):
    server, broker = meritz(safety, "US", "NASDAQ")
    placed = broker.place_order(req("US", "NASDAQ", order_type=kind, price=None))
    server.calls.clear()
    with pytest.raises(NotSupportedError):
        broker.amend_order(AmendRequest(order_id=placed.id, symbol=placed.symbol, market="US", venue="NASDAQ",
                                       side="buy", quantity=1, price="101", client_key="amend"))
    assert not [c for c in server.calls if c[0] == "POST"]


@pytest.mark.parametrize("damage", ["missing", "kind", "venue", "side", "continuation"])
def test_meritz_amend_requires_today_evidence(safety, damage):
    server, broker = meritz(safety, "US", "NASDAQ")
    placed = broker.place_order(req("US", "NASDAQ", order_type="loc"))
    if damage == "missing":
        server.today.clear()
    elif damage == "continuation":
        server.overrides["/trading/v1/overseas/orders/today"] = {
            "rsp_cd": "5766", "tr_cont": "1", "data": list(server.today.values())}
    else:
        field, value = {"kind": ("aspr_type_code", "2"), "venue": ("ovrs_exch_vndr_idno", "0321"),
                        "side": ("seln_byov_clcd", "1")}[damage]
        server.today[placed.id][field] = value
    server.calls.clear()
    with pytest.raises(InvalidResponseError):
        broker.amend_order(AmendRequest(order_id=placed.id, symbol=placed.symbol, market="US", venue="NASDAQ",
                                       side="buy", quantity=1, price="101", client_key="amend"))
    assert not [c for c in server.calls if c[0] == "POST"]


@pytest.mark.parametrize("kind", ["ioc", "fok"])
def test_after_session_alias_never_sends(safety, kind):
    server, broker = meritz(safety, "KR", "KRX")
    with pytest.raises(ValidationError):
        broker.place_order(req("KR", "KRX", symbol="005930", order_type=kind, session="after"))
    assert server.calls == []


@pytest.mark.parametrize("market,venue,vendor,currency", MERITZ_MARKETS[4:])
def test_meritz_sibling_exchange_orders_and_holdings_are_excluded(safety, market, venue, vendor, currency):
    server, broker = meritz(safety, market, venue)
    siblings = MERITZ_MARKETS[4:7] if currency == "JPY" else MERITZ_MARKETS[7:]
    expected_id = None
    for index, (other_market, other_venue, other_vendor, _) in enumerate(siblings):
        _, other = meritz(safety, other_market, other_venue)
        other._client = server
        placed = other.place_order(req(other_market, other_venue, client_key=f"sibling-{index}"))
        if other_market == market:
            expected_id = placed.id
        holding = server.example("GET", "/accounts/v1/me/overseas/holdings")["data"][0]
        holding.update(ovrs_stck_mrkt_tr_cls_code="05" if currency == "JPY" else "24", crcd=currency,
                       ovrs_stck_symb_iscd="0700", ovrs_exch_vndr_idno=other_vendor,
                       rmnd_dqty=str(index + 1), seln_able_dqty=str(index + 1))
        server.holdings["US"].append(holding)
    assert [o.id for o in broker.list_orders()] == [expected_id]
    [holding] = broker.get_positions()
    expected_qty = next(i + 1 for i, row in enumerate(siblings) if row[0] == market)
    assert holding.quantity == holding.sellable == broker.available_qty("0700") == expected_qty
    server.today.pop(expected_id)
    with pytest.raises(InvalidResponseError, match="lacks today evidence"):
        broker.list_orders()
    for holding in server.holdings["US"]:
        holding.pop("ovrs_exch_vndr_idno")
    with pytest.raises(InvalidResponseError, match="lacks known exchange evidence"):
        broker.get_positions()


@pytest.mark.parametrize("market,venue,vendor,currency", MERITZ_MARKETS[4:])
@pytest.mark.parametrize("damage", ["missing", "", "0000", "0537", None])
@pytest.mark.parametrize("surface", ["positions", "balance", "orders", "quantity", "symbol_orders"])
def test_meritz_unknown_shared_exchange_is_not_empty(safety, market, venue, vendor, currency, damage, surface):
    server, broker = meritz(safety, market, venue)
    server.overrides["/trading/v1/overseas/orders/estimate"] = {
        "rsp_cd": "5766", "tr_cont": "0",
        "data": [{"crcd": currency, "oder_crnc_oder_able_camt": "150"}]}
    placed = broker.place_order(req(market, venue))
    holding = server.example("GET", "/accounts/v1/me/overseas/holdings")["data"][0]
    holding.update(ovrs_stck_mrkt_tr_cls_code="05" if currency == "JPY" else "24", crcd=currency,
                   ovrs_stck_symb_iscd="0700", ovrs_exch_vndr_idno=vendor)
    server.holdings["US"].append(holding)
    for row in (holding, server.today[placed.id]):
        if damage == "missing":
            row.pop("ovrs_exch_vndr_idno")
        else:
            row["ovrs_exch_vndr_idno"] = damage
    read = {"positions": broker.get_positions, "balance": broker.get_balance, "orders": broker.list_orders,
            "quantity": lambda: broker.available_qty("0700"), "symbol_orders": lambda: broker.list_orders("0700")}
    with pytest.raises(InvalidResponseError, match="lacks known exchange evidence"):
        read[surface]()


@pytest.mark.parametrize("factory,markets", [(meritz, MERITZ_MARKETS[4:]), (kis_broker, KIS_MARKETS[3:])])
@pytest.mark.parametrize("field", ["price", "stop_price"])
def test_integer_currency_price_never_sends(safety, factory, markets, field):
    for market, venue, *_ in markets:
        if factory is meritz:
            server, broker = factory(safety, market, venue)
        else:
            broker, scope = factory(safety, market, venue)
        kw = {field: "1500.5"}
        if field == "stop_price":
            kw["order_type"] = "stop_limit"
        with pytest.raises(ConfigurationError, match="prices must be whole"):
            broker.place_order(req(market, venue, **kw))
        if factory is meritz:
            assert server.calls == []
        else:
            scope.client.request.assert_not_called()


@pytest.mark.parametrize("kind", ["moc", "stop_limit"])
def test_kiwoom_us_sell_only_kind_buy_never_sends(safety, kind):
    broker, client = make({"ust20000": [{"return_code": 0, "ord_no": "123456789"}]},
                          safety=safety, market="US", venue="NASDAQ")
    with pytest.raises(NotSupportedError):
        broker.place_order(req("US", "NASDAQ", order_type=kind, price=None if kind == "moc" else "100",
                               **({"stop_price": "99"} if kind == "stop_limit" else {})))
    assert client.calls == []
