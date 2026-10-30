"""KIS US mock account: open orders / order status / single cancel through the official ledger TR.

Official basis (koreainvestment/open-trading-api, examples_llm/overseas_stock):
* inquire_nccs.py:92 -- TTTS3018R only, no mock TR (so no live open-order snapshot on mock);
* inquire_ccnl.py:127-131 -- TTTS3035R real / VTTS3035R mock, mock takes only all-rows filters;
* order_rvsecncl.py:102-106 -- TTTT1004U real / VTTT1004U mock.
"""

from decimal import Decimal

import pytest

from pykbroker.brokers.kis import KIS
from pykbroker.exceptions import ConfigurationError, InvalidResponseError, NotSupportedError
from pykbroker.models import CancelRequest, Order

from .test_virtual_vendor_chain import _us


def _row(odno, qty, filled, remaining, *, pdno="TEST", cancel="00", orgn="", branch="06010"):
    return {"odno": odno, "pdno": pdno, "ft_ord_qty": str(qty), "ft_ccld_qty": str(filled), "nccs_qty": str(remaining),
            "rvse_cncl_dvsn": cancel, "orgn_odno": orgn, "ord_gno_brno": branch, "ord_dt": "20261030",
            "ord_tmd": "130000", "ovrs_excg_cd": "NASD"}


def _broker(*pages, live_safety=None, virtual=True):
    broker, client, api = _us(virtual, **({"safety": live_safety} if live_safety else {}))
    client.scope.us_ledger_pages = [list(p) for p in pages] or [[]]
    return broker, client.scope


def test_zero_open_orders():
    broker, scope = _broker([_row("1", 2, 2, 0)])
    assert broker.list_orders() == []
    request = scope.requests[0]
    assert request["tr_id"] == "VTTS3035R"
    p = request["params"]
    # mock-only filters per the official doc: all rows, no symbol/exchange, no order-number search
    assert (p["PDNO"], p["SLL_BUY_DVSN"], p["CCLD_NCCS_DVSN"], p["OVRS_EXCG_CD"], p["ODNO"]) == ("", "00", "00", "", "")
    assert p["ORD_STRT_DT"] < p["ORD_END_DT"] and len(p["ORD_STRT_DT"]) == 8


def test_one_open_order_and_symbol_filter():
    broker, _ = _broker([_row("7", 3, 0, 3), _row("8", 1, 0, 1, pdno="OTHER")])
    orders = broker.list_orders("TEST")
    assert [(o.id, o.symbol, o.quantity, o.filled, o.remaining, o.status, o.virtual, o.market) for o in orders] == [
        ("7", "TEST", 3, 0, 3, "open", True, "US")]
    assert len(broker.list_orders()) == 2


def test_partial_fill_is_partial():
    broker, _ = _broker([_row("7", 3, 1, 2)])
    (order,) = broker.list_orders()
    assert (order.filled, order.remaining, order.status) == (1, 2, "partial")
    assert broker.status(Order(id="7", symbol="TEST", market="US")).status == "partial"


def test_cancelled_order_is_not_open_and_status_is_cancelled():
    # trader observation: after an accepted cancel the original row's nccs_qty is 0
    broker, _ = _broker([_row("7", 3, 0, 0), _row("9", 3, 0, 0, cancel="02", orgn="7")])
    assert broker.list_orders() == []
    assert broker.status(Order(id="7", symbol="TEST", market="US")).status == "cancelled"


def test_cancel_rows_never_decide_a_live_original_stays_open():
    # nccs_qty of the original is the evidence: any cancel row (accepted, pending or rejected) is ignored
    for extra in ({}, {"rjct_rson": "1234"}, {"prcs_stat_name": "접수"}, {"prcs_stat_name": "거부"}):
        broker, _ = _broker([_row("7", 3, 0, 3), {**_row("9", 3, 0, 0, cancel="02", orgn="7"), **extra}])
        assert [o.id for o in broker.list_orders()] == ["7"]
        assert broker.status(Order(id="7", symbol="TEST", market="US")).status == "open"


def test_cancel_row_on_a_fully_filled_order_stays_filled():
    broker, _ = _broker([_row("7", 3, 3, 0), _row("9", 3, 0, 0, cancel="02", orgn="7")])
    assert broker.list_orders() == []
    assert broker.status(Order(id="7", symbol="TEST", market="US")).status == "filled"


def test_same_order_number_on_two_days_is_keyed_by_date():
    yesterday = {**_row("7", 3, 0, 0), "ord_dt": "20261029"}          # cancelled yesterday
    today = {**_row("7", 5, 0, 5), "ord_dt": "20261030"}              # live today, same number
    broker, _ = _broker([yesterday, today])
    (live,) = broker.list_orders()
    assert (live.id, live.order_date, live.remaining) == ("7", "20261030", 5)
    got = broker.status(Order(id="7", symbol="TEST", market="US", order_date="20261030"))
    assert (got.status, got.quantity) == ("open", 5)
    old = broker.status(Order(id="7", symbol="TEST", market="US", order_date="20261029"))
    assert (old.status, old.quantity) == ("cancelled", 3)
    # no date and two candidate days: never guess
    assert broker.status(Order(id="7", symbol="TEST", market="US")).status == "unknown"


def test_cancel_matches_only_the_open_row_not_yesterdays_same_number(live_safety):
    yesterday = {**_row("7", 3, 3, 0), "ord_dt": "20261029"}
    today = {**_row("7", 2, 0, 2), "ord_dt": "20261030"}
    broker, scope = _broker([yesterday, today], live_safety=live_safety)
    broker.cancel_order(CancelRequest(order_id="7", symbol="TEST", quantity=2, client_key="c9",
                                      market="US", venue="NASDAQ"))
    assert scope.cancels[0]["qty"] == 2


def test_ledger_window_starts_one_day_before_today():
    broker, scope = _broker([_row("1", 2, 2, 0)])
    broker.list_orders()
    p = scope.requests[0]["params"]
    from datetime import datetime
    assert (datetime.strptime(p["ORD_END_DT"], "%Y%m%d") - datetime.strptime(p["ORD_STRT_DT"], "%Y%m%d")).days == 1


def test_status_filled_and_absent():
    broker, _ = _broker([_row("7", 2, 2, 0)])
    assert broker.status(Order(id="7", symbol="TEST", market="US")).status == "filled"
    assert broker.status(Order(id="404", symbol="TEST", market="US")).status == "unknown"


def test_pagination_follows_the_continuation_and_dedups_a_repeated_row():
    broker, scope = _broker([_row("1", 1, 0, 1)], [_row("1", 1, 0, 1), _row("2", 1, 0, 1)])
    assert [o.id for o in broker.list_orders()] == ["1", "2"]
    assert [r["headers"]["tr_cont"] for r in scope.requests] == ["", "N"]
    assert scope.requests[1]["params"]["CTX_AREA_NK200"] == "1"


def test_inconsistent_quantities_are_refused():
    broker, _ = _broker([_row("1", 1, 1, 1)])
    with pytest.raises(InvalidResponseError):
        broker.list_orders()


def test_single_cancel_uses_the_exact_row_and_the_mock_cancel_tr(live_safety):
    broker, scope = _broker([_row("7", 3, 1, 2, branch="06010"), _row("8", 1, 0, 1)], live_safety=live_safety)
    assert broker.capabilities().supports_single_order_cancel is True
    result = broker.cancel_order(CancelRequest(order_id="7", symbol="TEST", quantity=2, client_key="c1",
                                               market="US", venue="NASDAQ"))
    assert result.status == "accepted" and result.id == "7" and result.virtual is True
    assert scope.cancels == [{"tr_id": "VTTT1004U", "market": "NASD", "code": "TEST", "odno": "7",
                              "branch": "06010", "qty": 2}]


def test_cancel_more_than_remaining_or_unknown_order_is_refused(live_safety):
    broker, scope = _broker([_row("7", 3, 1, 2)], live_safety=live_safety)
    with pytest.raises(ConfigurationError):
        broker.cancel_order(CancelRequest(order_id="7", symbol="TEST", quantity=3, client_key="c2",
                                          market="US", venue="NASDAQ"))
    with pytest.raises(NotSupportedError):
        broker.cancel_order(CancelRequest(order_id="55", symbol="TEST", quantity=1, client_key="c3",
                                          market="US", venue="NASDAQ"))
    assert scope.cancels == []


def test_real_us_status_ignores_cancel_rows_like_base():
    # base reads only the matched row's own rvse_cncl_dvsn: a cancel row naming a filled original is no cancel
    broker, client, api = _us(False)
    rows = [_row("42", 3, 3, 0), _row("43", 3, 0, 0, cancel="02", orgn="42")]
    api.get_orders_overseas_all = lambda **_: {"output": rows}
    assert broker.status(Order(id="42", symbol="TEST", market="US")).status == "filled"


def test_real_us_path_is_unchanged():
    broker, client, api = _us(False)
    assert [o.id for o in broker.list_orders()] == ["42"]
    assert api.calls == 1 and client.scope.requests == []
    assert isinstance(broker, KIS)


def test_real_us_partial_fill_with_nothing_open_keeps_base_verdict():
    # real account: only the row's own rvse_cncl_dvsn decides; partial fill + nccs 0 is not "cancelled"
    broker, client, api = _us(False)
    api.get_orders_overseas_all = lambda **_: {"output": [_row("42", 3, 1, 0)]}
    assert broker.status(Order(id="42", symbol="TEST", market="US")).status == "unknown"
    mock, _ = _broker([_row("42", 3, 1, 0)])
    assert mock.status(Order(id="42", symbol="TEST", market="US")).status == "cancelled"


def test_us_mock_cash_uses_the_official_mock_tr_not_the_real_margin_tr():
    broker, scope = _broker()
    scope.us_cash_rows = [{"crcy_cd": "USD", "frcr_dncl_amt_2": "100.5", "frcr_drwg_psbl_amt_1": "80"}]
    assert broker.available_cash() == Decimal("80")
    assert [r["tr_id"] for r in scope.requests] == ["VTRP6504R"]
    assert scope.requests[0]["path"] == "/uapi/overseas-stock/v1/trading/inquire-present-balance"


def test_us_mock_cash_needs_exactly_one_usd_row():
    broker, scope = _broker()
    scope.us_cash_rows = []
    with pytest.raises(InvalidResponseError):
        broker.available_cash()


def test_mock_integrated_buying_power_is_an_explicit_unsupported():
    broker, scope = _broker()
    broker.is_integrated = True
    with pytest.raises(NotSupportedError):
        broker.available_cash()
    assert scope.requests == []


def test_us_mock_get_balance_reads_mock_cash_without_provider():
    broker, scope = _broker()
    broker._cash_provider = None
    scope.us_cash_rows = [{"crcy_cd": "USD", "frcr_dncl_amt_2": "100.5", "frcr_drwg_psbl_amt_1": "80"}]
    balance = broker.get_balance()
    assert (balance.currency, balance.cash_available) == ("USD", Decimal("80"))
    assert "VTRP6504R" in [r["tr_id"] for r in scope.requests]


def test_us_mock_get_balance_missing_usd_row_fails_closed():
    broker, scope = _broker()
    broker._cash_provider = None
    scope.us_cash_rows = []
    with pytest.raises(InvalidResponseError):
        broker.get_balance()


def test_us_mock_get_balance_rt_cd_failure_fails_closed():
    broker, scope = _broker()
    broker._cash_provider = None
    orig = scope._request

    def bad(method, path, **kw):
        r = orig(method, path, **kw)
        return type(r)(status_code=200, headers=r.headers, json=lambda: {"rt_cd": "1", "msg_cd": "X", "msg1": "no"})

    scope.client.request = bad
    with pytest.raises(Exception) as exc:
        broker.get_balance()
    assert not isinstance(exc.value, ConfigurationError)


def test_us_mock_get_balance_caller_provider_wins():
    broker, scope = _broker()
    broker._cash_provider = lambda _scope: Decimal("7")
    assert broker.get_balance().cash_available == Decimal("7")
    assert "VTRP6504R" not in [r["tr_id"] for r in scope.requests]


def test_us_real_get_balance_never_uses_mock_tr():
    broker, client, api = _us(False)
    broker._cash_provider = None
    called = []
    broker._us_native_cash = lambda scope: called.append(1) or Decimal("5")
    broker._us_mock_cash = lambda scope: (_ for _ in ()).throw(AssertionError("mock TR on real"))
    assert broker.get_balance().cash_available == Decimal("5") and called
