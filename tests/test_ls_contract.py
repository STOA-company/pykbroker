"""LS synthetic contracts. UNVERIFIED: no official detail or broker recording.

All enum values are deliberately fictitious, injected only for offline requests.
cheqty/cheprice/qty/price are candidate t0425 fields, not verified LS semantics.
The module lock is changed by monkeypatch solely to exercise the fake transport.
"""

from copy import deepcopy
from decimal import Decimal
from importlib import import_module
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from pykbroker.brokers import ls as module
from pykbroker.brokers.ls import LS
from pykbroker.exceptions import (
    BrokerRejectedError,
    ConfigurationError,
    DryRunError,
    DuplicateOrderError,
    InvalidResponseError,
    NotSupportedError,
    RiskCapError,
    SpecUnverifiedError,
    TransportError,
)
from pykbroker.factory import create_broker
from pykbroker.models import CancelRequest, Order, OrderRequest
from pykbroker.safety.rate_limit import RateLimiter
from tests.conftest import load_fixture

CODES = {
    name: "fixture-" + name
    for name in (
        "exchange",
        "balance_type",
        "prcgb",
        "chegb",
        "dangb",
        "charge",
        "order_filter",
        "side_all",
        "sort_order",
        "buy",
        "sell",
        "limit",
        "market",
        "cash",
        "condition",
        "member",
    )
}


class FakeClient:
    def __init__(self, responses):
        self.responses = {tr: list(pages) for tr, pages in responses.items()}
        self.calls = []

    def post(self, path, *, headers, json):
        self.calls.append((path, headers, json))
        response = self.responses[headers["tr_cd"]].pop(0)
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


def make(responses=None, **kwargs):
    client = FakeClient(responses or {})
    limiter = Mock(spec=RateLimiter)
    broker = LS(account="fixture-account", client=client, codes=CODES, rate_limiter=limiter, **kwargs)
    return broker, client, limiter


def buy(**changes):
    return OrderRequest(
        **{
            "symbol": "005930",
            "side": "buy",
            "quantity": 1,
            "price": "70000",
            "client_key": "buy-1",
            **changes,
        }
    )


def cancel(**changes):
    return CancelRequest(
        **{
            "order_id": "42",
            "symbol": "005930",
            "quantity": 1,
            "client_key": "cancel-1",
            **changes,
        }
    )


def page(tr, **changes):
    return {**load_fixture("ls", tr), **changes}


@pytest.fixture
def unlocked(monkeypatch):
    monkeypatch.setattr(module, "LS_SPEC_VERIFIED", True)


def test_ls_is_factory_constructible_but_remains_locked():
    broker = create_broker("LS", account="fixture-account")
    assert type(broker) is LS
    assert module.LS_SPEC_VERIFIED is False
    assert import_module("pykbroker.experimental.ls") is module
    caps = broker.capabilities()
    assert caps.markets == ("KR",) and caps.venues == ("KRX",)
    assert not any(
        (
            caps.supports_spot,
            caps.supports_limit_order,
            caps.supports_market_order,
            caps.supports_single_order_cancel,
            caps.supports_order_history,
            caps.realtime_open_orders,
            caps.supports_virtual_account,
            caps.supports_nxt,
        )
    )


@pytest.mark.parametrize("operation", ["place", "place_order", "cancel", "cancel_order"])
@pytest.mark.parametrize("flag", [False, None, 1, "True"])
def test_every_write_requires_exact_verified_flag(monkeypatch, live_safety, operation, flag):
    monkeypatch.setattr(module, "LS_SPEC_VERIFIED", flag)
    broker, client, limiter = make(safety=live_safety)
    with pytest.raises(SpecUnverifiedError):
        getattr(broker, operation)(buy() if operation.startswith("place") else cancel())
    assert client.calls == []
    limiter.wait.assert_not_called()


def test_candidate_t0425_fields_keep_fill_price_separate_from_limit_price():
    broker, client, limiter = make({"t0425": [page("t0425")]})
    orders = broker.list_orders()
    assert orders[0].id == "42" and orders[0].symbol == "005930"
    assert orders[0].price == Decimal("70000")
    assert orders[0].fill_price == Decimal("69000.25")
    assert (orders[0].quantity, orders[0].filled, orders[0].remaining, orders[0].status) == (3, 1, 2, "unknown")
    assert orders[1].filled == 3 and orders[1].fill_price == Decimal("68900")
    assert orders[1].status == "unknown"  # UNVERIFIED semantics cannot arm a chain
    assert orders[2].remaining == 0 and orders[2].filled == 0 and orders[2].fill_price is None
    assert orders[2].status == "unknown"
    path, headers, body = client.calls[0]
    assert path == "/stock/accno" and headers["tr_cd"] == "t0425"
    assert body == {
        "t0425InBlock": {
            "expcode": "",
            "chegb": "fixture-order_filter",
            "medosu": "fixture-side_all",
            "sortgb": "fixture-sort_order",
            "cts_ordno": "",
        }
    }
    limiter.wait.assert_called_once_with("t0425")


def test_quote_balance_and_positions_reuse_existing_fields():
    broker, client, limiter = make(
        {
            "t1102": [page("t1102")],
            "CSPAQ12200": [page("CSPAQ12200")],
            "t0424": [page("t0424")],
        }
    )
    quote = broker.get_quote("A005930")
    assert (quote.symbol, quote.last, quote.bid, quote.ask) == ("005930", Decimal(70000), None, None)
    balance = broker.get_balance()
    assert (balance.currency, balance.cash_available, balance.position_value) == (
        "KRW",
        Decimal(200000),
        Decimal(210000),
    )
    (position,) = broker.get_positions()
    assert (position.symbol, position.quantity, position.sellable) == ("005930", 3, 2)
    assert position.average_price == Decimal("68000.25")
    assert [call.args[0] for call in limiter.wait.call_args_list] == ["t1102", "CSPAQ12200", "t0424"]
    assert client.calls[0][2] == {"t1102InBlock": {"shcode": "005930", "exchgubun": "fixture-exchange"}}
    assert client.calls[1][2] == {"CSPAQ12200InBlock1": {"BalCreTp": "fixture-balance_type"}}
    assert client.calls[2][2] == {
        "t0424InBlock": {
            "prcgb": "fixture-prcgb",
            "chegb": "fixture-chegb",
            "dangb": "fixture-dangb",
            "charge": "fixture-charge",
            "cts_expcode": "",
        }
    }
    for _, headers, _ in client.calls:
        assert headers == {
            "content-type": "application/json",
            "tr_cd": headers["tr_cd"],
            "tr_cont": "N",
            "tr_cont_key": "",
        }


@pytest.mark.parametrize(
    "tr,method,block",
    [
        ("t0424", "get_positions", "t0424OutBlock1"),
        ("t0425", "list_orders", "t0425OutBlock1"),
    ],
)
def test_empty_account_pages_are_empty_lists(tr, method, block):
    broker, _, _ = make({tr: [page(tr, **{block: []})]})
    assert getattr(broker, method)() == []


@pytest.mark.parametrize(
    "tr,method,block,cursor",
    [
        ("t0424", "get_positions", "t0424OutBlock", "cts_expcode"),
        ("t0425", "list_orders", "t0425OutBlock", "cts_ordno"),
    ],
)
@pytest.mark.parametrize(
    "meta,expected",
    [
        ("more", SpecUnverifiedError),
        (None, InvalidResponseError),
        (7, InvalidResponseError),
    ],
)
def test_unverified_or_unreadable_pagination_never_returns_partial_data(tr, method, block, cursor, meta, expected):
    broker, client, _ = make({tr: [page(tr, **{block: {cursor: meta}})]})
    with pytest.raises(expected):
        getattr(broker, method)()
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "tr,method,block",
    [
        ("t0424", "get_positions", "t0424OutBlock"),
        ("t0425", "list_orders", "t0425OutBlock"),
    ],
)
def test_missing_pagination_evidence_does_not_mean_last_page(tr, method, block):
    body = page(tr)
    del body[block]
    with pytest.raises(InvalidResponseError):
        getattr(make({tr: [body]})[0], method)()


@pytest.mark.parametrize(
    "tr,method,block",
    [
        ("t0424", "get_positions", "t0424OutBlock1"),
        ("t0425", "list_orders", "t0425OutBlock1"),
    ],
)
@pytest.mark.parametrize("rows", [None, {}, "", [None], ["row"]])
def test_malformed_rows_never_become_empty_accounts(tr, method, block, rows):
    with pytest.raises(InvalidResponseError):
        getattr(make({tr: [page(tr, **{block: rows})]})[0], method)()


@pytest.mark.parametrize("missing", ["MnyOrdAbleAmt", "BalEvalAmt"])
def test_missing_account_money_is_not_zero(missing):
    body = page("CSPAQ12200")
    del body["CSPAQ12200OutBlock2"][missing]
    with pytest.raises(InvalidResponseError):
        make({"CSPAQ12200": [body]})[0].get_balance()


@pytest.mark.parametrize(
    "field,value",
    [
        ("janqty", "1.5"),
        ("mdposqt", "4"),
        ("pamt", "NaN"),
        ("appamt", "-1"),
        ("price", False),
    ],
)
def test_invalid_holdings_cannot_become_sellable(field, value):
    body = page("t0424")
    body["t0424OutBlock1"][0][field] = value
    with pytest.raises(InvalidResponseError):
        make({"t0424": [body]})[0].get_positions()


@pytest.mark.parametrize("price", [None, 0, "NaN", "Infinity", "-1", "bad", True])
def test_invalid_quotes_do_not_become_zero_prices(price):
    with pytest.raises(InvalidResponseError):
        make({"t1102": [page("t1102", t1102OutBlock={"price": price})]})[0].get_quote("005930")


@pytest.mark.parametrize("missing", ["qty", "cheqty", "cheprice", "price"])
def test_missing_candidate_order_fields_remain_unknown(missing):
    body = page("t0425")
    row = body["t0425OutBlock1"][0]
    del row[missing]
    order = make({"t0425": [body]})[0].list_orders()[0]
    assert order.status == "unknown"
    if missing in ("cheqty", "cheprice"):
        assert order.fill_price is None
    if missing == "cheqty":
        assert order.filled is None  # Never qty - ordrem.
    if missing == "qty":
        assert order.quantity is None
    if missing == "price":
        assert order.price is None


@pytest.mark.parametrize("value", [None, "", "0", 0])
def test_missing_or_zero_fill_price_never_falls_back_to_order_price(value):
    body = page("t0425")
    body["t0425OutBlock1"][0]["cheprice"] = value
    order = make({"t0425": [body]})[0].list_orders()[0]
    assert order.filled == 1 and order.fill_price is None and order.price == Decimal(70000)


@pytest.mark.parametrize(
    "field,value",
    [
        ("qty", "0"),
        ("qty", "0.5"),
        ("cheqty", "4"),
        ("cheqty", True),
        ("cheqty", "1.5"),
        ("cheqty", "-1"),
        ("ordrem", "4"),
        ("ordrem", None),
        ("cheprice", "NaN"),
        ("cheprice", "-1"),
        ("cheprice", False),
        ("price", False),
        ("ordno", "0"),
        ("ordno", True),
        ("expcode", "bad-symbol"),
    ],
)
def test_bad_order_fields_fail_the_entire_page_even_before_symbol_filter(field, value):
    body = page("t0425")
    body["t0425OutBlock1"][2][field] = value
    with pytest.raises(InvalidResponseError):
        make({"t0425": [body]})[0].list_orders("005930")


def test_normalized_symbol_filter_and_missing_status_never_infer_a_fill():
    broker, client, _ = make({"t0425": [page("t0425"), page("t0425")]})
    assert len(broker.list_orders("A005930")) == 2
    assert client.calls[0][2]["t0425InBlock"]["expcode"] == "005930"
    old = Order(id="999", symbol="005930", status="accepted")
    missing = broker.status(old)
    assert missing.status == "unknown" and missing.filled is None and missing.fill_price is None


def test_candidate_status_is_only_interpreted_after_explicit_spec_verification(unlocked):
    body = page("t0425")
    body["t0425OutBlock1"].append(
        {
            "ordno": "46",
            "expcode": "005930",
            "qty": "3",
            "cheqty": "0",
            "ordrem": "3",
        }
    )
    orders = make({"t0425": [body]})[0].list_orders()
    assert [order.status for order in orders] == ["partial", "filled", "unknown", "open"]
    assert orders[3].fill_price is None


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_order_request_reuses_legacy_tr_and_injected_enum_values(unlocked, live_safety, side):
    broker, client, limiter = make({"CSPAT00601": [page("CSPAT00601")]}, safety=live_safety)
    result = broker.place_order(buy(symbol="A005930", side=side))
    assert result == Order(
        id="42", symbol="005930", side=side, quantity=1, order_type="limit", price=Decimal(70000), status="accepted"
    )
    path, headers, body = client.calls[0]
    assert path == "/stock/order" and headers["tr_cd"] == "CSPAT00601"
    assert body == {
        "CSPAT00601InBlock1": {
            "IsuNo": "005930",
            "OrdQty": 1,
            "OrdPrc": 70000,
            "BnsTpCode": "fixture-" + side,
            "OrdprcPtnCode": "fixture-limit",
            "MgntrnCode": "fixture-cash",
            "LoanDt": "",
            "OrdCndiTpCode": "fixture-condition",
            "MbrNo": "fixture-member",
        }
    }
    limiter.wait.assert_called_once_with("CSPAT00601")
    assert result.filled is None and result.fill_price is None


def test_exact_single_cancel_never_lists_or_expands_other_orders(unlocked, live_safety):
    broker, client, limiter = make({"CSPAT00801": [page("CSPAT00801")]}, safety=live_safety)
    result = broker.cancel_order(cancel(symbol="A005930", quantity=2))
    assert result == Order(id="42", symbol="005930", status="accepted")
    path, headers, body = client.calls[0]
    assert path == "/stock/order" and headers["tr_cd"] == "CSPAT00801"
    assert body == {"CSPAT00801InBlock1": {"OrgOrdNo": "42", "IsuNo": "005930", "OrdQty": 2}}
    limiter.wait.assert_called_once_with("CSPAT00801")
    assert result.status != "cancelled"  # The acknowledgement is not closing evidence.


@pytest.mark.parametrize("operation", ["place_order", "cancel_order"])
def test_default_dry_run_remains_closed_even_with_spec_flag(unlocked, operation):
    broker, client, limiter = make()
    with pytest.raises(DryRunError):
        getattr(broker, operation)(buy() if operation.startswith("place") else cancel())
    assert client.calls == []
    limiter.wait.assert_not_called()


@pytest.mark.parametrize("code", ["buy", "sell", "limit", "cash", "condition", "member"])
def test_spec_flag_alone_never_fills_missing_order_enums(unlocked, live_safety, code):
    broker, client, limiter = make(safety=live_safety)
    del broker._codes[code]
    with pytest.raises(ConfigurationError, match="code"):
        broker.place_order(buy(side="sell" if code == "sell" else "buy"))
    assert client.calls == []
    limiter.wait.assert_not_called()


@pytest.mark.parametrize("code,value", [("exchange", ""), ("exchange", " "), ("exchange", 1)])
def test_unusable_read_codes_never_reach_the_wire(code, value):
    broker, client, limiter = make()
    broker._codes[code] = value
    with pytest.raises(ConfigurationError):
        broker.get_quote("005930")
    assert client.calls == []
    limiter.wait.assert_not_called()


def test_supplied_rate_budget_is_mandatory_and_precedes_post(unlocked, live_safety):
    broker, client, _ = make(safety=live_safety)
    broker._limiter = RateLimiter({})
    with pytest.raises(ConfigurationError, match="rate"):
        broker.place_order(buy())
    assert client.calls == []
    broker, client, limiter = make({"CSPAT00601": [page("CSPAT00601")]}, safety=live_safety)
    events = []
    limiter.wait.side_effect = lambda tr: events.append(("wait", tr))
    post = client.post
    client.post = lambda path, **kwargs: events.append(("post", kwargs["headers"]["tr_cd"])) or post(path, **kwargs)
    broker.place_order(buy(client_key="second"))
    assert events == [("wait", "CSPAT00601"), ("post", "CSPAT00601")]


@pytest.mark.parametrize("operation,tr", [("place_order", "CSPAT00601"), ("cancel_order", "CSPAT00801")])
def test_duplicate_client_key_is_one_post(unlocked, live_safety, operation, tr):
    broker, client, _ = make({tr: [page(tr), page(tr)]}, safety=live_safety)
    request = buy() if operation.startswith("place") else cancel()
    getattr(broker, operation)(request)
    with pytest.raises(DuplicateOrderError):
        getattr(broker, operation)(request)
    assert len(client.calls) == 1


def test_risk_cap_refuses_before_post_and_accepts_exact_boundary(unlocked, live_safety):
    broker, client, _ = make({"CSPAT00601": [page("CSPAT00601")]}, safety=live_safety)
    with pytest.raises(RiskCapError):
        broker.place_order(buy(price="100001"))
    assert client.calls == []
    broker.place_order(buy(price="100000", client_key="boundary"))
    assert len(client.calls) == 1


@pytest.mark.parametrize("operation,tr", [("place_order", "CSPAT00601"), ("cancel_order", "CSPAT00801")])
@pytest.mark.parametrize(
    "response,expected",
    [
        (OSError("fixture-sensitive-detail"), TransportError),
        (TimeoutError("timeout"), TransportError),
        (ValueError("fixture-sensitive-detail"), InvalidResponseError),
        (None, InvalidResponseError),
        ([], InvalidResponseError),
        ({}, InvalidResponseError),
        ({"rsp_cd": None}, InvalidResponseError),
        ({"rsp_cd": True}, InvalidResponseError),
        ({"rsp_cd": 0}, InvalidResponseError),
        ({"rsp_cd": "00000"}, InvalidResponseError),
        ({"rsp_cd": "ERROR", "rsp_msg": "fixture-sensitive-detail"}, InvalidResponseError),
    ],
)
def test_uncertain_writes_never_retry_or_release_the_client_key(
    unlocked, live_safety, operation, tr, response, expected
):
    broker, client, _ = make({tr: [response, page(tr)]}, safety=live_safety)
    request = buy() if operation.startswith("place") else cancel()
    with pytest.raises(expected) as raised:
        getattr(broker, operation)(request)
    assert "fixture-sensitive-detail" not in str(raised.value)
    assert not isinstance(raised.value, BrokerRejectedError)
    with pytest.raises(DuplicateOrderError):
        getattr(broker, operation)(request)
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "operation,tr,block",
    [
        ("place_order", "CSPAT00601", "CSPAT00601OutBlock2"),
        ("cancel_order", "CSPAT00801", "CSPAT00801OutBlock2"),
    ],
)
@pytest.mark.parametrize("order_id", [None, "", " ", "0", "0000", -1, True, 42.1, "１２", "abc"])
def test_missing_or_invalid_write_acknowledgement_is_unknown(unlocked, live_safety, operation, tr, block, order_id):
    broker, client, _ = make({tr: [page(tr, **{block: {"OrdNo": order_id}})]}, safety=live_safety)
    with pytest.raises(InvalidResponseError):
        getattr(broker, operation)(buy() if operation.startswith("place") else cancel())
    assert len(client.calls) == 1


@pytest.mark.parametrize("order_id", ["", " ", "0", "0000", "-1", "１２", "abc"])
def test_bad_original_order_number_is_refused_before_cancel(unlocked, live_safety, order_id):
    broker, client, _ = make(safety=live_safety)
    with pytest.raises((ConfigurationError, ValidationError)):
        broker.cancel_order(cancel(order_id=order_id))
    assert client.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"market": "US"},
        {"venue": "NXT"},
        {"venue": "SOR"},
        {"order_type": "close"},
        {"price": "70000.5"},
    ],
)
def test_unsupported_order_routes_are_refused_before_post(unlocked, live_safety, changes):
    broker, client, _ = make(safety=live_safety)
    with pytest.raises((NotSupportedError, ConfigurationError)):
        broker.place_order(buy(**changes))
    assert client.calls == []


def test_market_order_still_needs_existing_safety_authorization(unlocked, live_safety):
    broker, client, _ = make({"CSPAT00601": [page("CSPAT00601")]}, safety=live_safety)
    request = buy(order_type="market", price=None)
    with pytest.raises(RiskCapError):
        broker.place_order(request)
    assert client.calls == []
    live_safety.market_order_allowed = lambda request: True
    live_safety.valuation = lambda request: Decimal(70000)
    result = broker.place_order(request)
    assert result.order_type == "market" and result.price is None
    assert client.calls[0][2]["CSPAT00601InBlock1"]["OrdPrc"] == 0
    assert client.calls[0][2]["CSPAT00601InBlock1"]["OrdprcPtnCode"] == "fixture-market"


def test_factory_never_infers_an_unlock_from_environment(monkeypatch, live_safety):
    monkeypatch.setenv("LS_SPEC_VERIFIED", "True")
    monkeypatch.setenv("PYKBROKER_CLIENT_OPTIONS", '{"LS_SPEC_VERIFIED": true}')
    broker = create_broker("ls", account="fixture-account", safety=live_safety)
    with pytest.raises(SpecUnverifiedError):
        broker.place_order(buy())
    with pytest.raises(ConfigurationError):
        broker.get_quote("005930")


def test_close_blocks_reads_without_owning_the_injected_client():
    broker, client, limiter = make({"t1102": [page("t1102")]})
    client.close = Mock()
    broker.close()
    broker.close()
    with pytest.raises(ConfigurationError, match="closed"):
        broker.get_quote("005930")
    assert client.calls == []
    client.close.assert_not_called()
    limiter.wait.assert_not_called()


def test_us_and_virtual_are_not_advertised_or_constructible():
    with pytest.raises(NotSupportedError):
        LS(account="fixture-account", market="US")
    with pytest.raises((TypeError, NotSupportedError)):
        LS(account="fixture-account", virtual=True)
    assert not LS(account="fixture-account").capabilities().supports_virtual_account
