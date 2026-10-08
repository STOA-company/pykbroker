"""NH domestic cash orders and daily ledger, with an offline transport only."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pykbroker.brokers.nh import NH, NH_MOCK_BASE_URL
from pykbroker.exceptions import (
    BrokerRejectedError,
    ConfigurationError,
    InvalidResponseError,
    NotSupportedError,
    TransportError,
)
from pykbroker.factory import BROKERS
from pykbroker.models import CancelRequest, Order, OrderRequest
from pykbroker.safety.rate_limit import RateLimiter

ORDER = "/krstock/order/v1/"
DAILY = "/krstock/inquiry/v1/dailyOrderExecution"
NOW = datetime(2026, 9, 26, 16, 0, tzinfo=UTC)  # 2026-09-27 in Seoul


def reply(output=None, *, code="00000", message="정상처리되었습니다", status=200, headers=None):
    body = {"message": {"usr_msg": message}}
    if code is not None:
        body["rsp_cd"] = code
    if output is not None:
        body["Output_0"] = output
    return status, body, headers or {}


def make(script, *, safety=None, virtual=False, venue="KRX", **kwargs):
    calls = []
    queue = {path: list(responses) for path, responses in script.items()}

    class Fake:
        def __init__(self, base_url):
            self.base_url = base_url

        def post(self, path, *, headers, data):
            calls.append((self.base_url, path, dict(headers), json.loads(data)))
            response = queue[path].pop(0)
            if isinstance(response, Exception):
                raise response
            return response

    broker = NH(
        account="12345678901",
        client_options={
            "app_key": "secret-key",
            "app_secret": "secret-value",
            "token": "secret-token",
            "acct_type": "03" if virtual else "01",
        },
        client_factory=Fake,
        allow_live_orders=True,  # Fake transport; production defaults remain locked.
        clock=lambda: NOW,
        rate_limiter=RateLimiter({"read": 10000}),
        safety=safety,
        virtual=virtual,
        venue=venue,
        **kwargs,
    )
    return broker, calls


def order(**changes):
    return OrderRequest(
        **{"symbol": "A005930", "side": "buy", "quantity": 1, "price": "70000", "client_key": "order-key", **changes}
    )


def cancel(**changes):
    return CancelRequest(
        **{"order_id": "mkt:123", "symbol": "005930", "quantity": 2, "client_key": "cancel-key", **changes}
    )


def test_cash_buy_and_sell_exact_wire_contract_and_mock_host(live_safety):
    broker, calls = make(
        {
            ORDER + "cashBuy": [reply({"mkt_orr_no": 123})],
            ORDER + "cashSell": [reply({"mkt_orr_no": 124})],
        },
        safety=live_safety,
        virtual=True,
    )
    bought = broker.place_order(order())
    sold = broker.place_order(order(side="sell", quantity=1, price="15000", client_key="sell-key"))
    assert BROKERS["nh"] is NH
    assert (bought.id, bought.status, bought.virtual) == ("mkt:123", "accepted", True)
    assert (sold.id, sold.side, sold.price) == ("mkt:124", "sell", Decimal(15000))
    assert [call[1] for call in calls] == [ORDER + "cashBuy", ORDER + "cashSell"]
    assert all(call[0] == NH_MOCK_BASE_URL for call in calls)
    assert calls[0][3] == {
        "Input_0": {
            "act_no": "12345678901",
            "iem_cd": "005930",
            "orr_qty": 1,
            "orr_pr": 70000,
            "orr_amt": 70000,
            "nmn_pr_tp_cd": "01",
            "orr_cnd_dit_cd": "00",
            "ssl_nmn_pr_dit_cd": "00",
            "rmt_mkt_cd": "KRX",
            "sor_mkt_sli_yn": "N",
        }
    }
    assert calls[1][3]["Input_0"]["orr_amt"] == 15000
    assert all("x-client-secret" not in call[2] for call in calls)


@pytest.mark.parametrize(
    "changes,error",
    [({"order_type": "market", "price": None}, NotSupportedError), ({"price": "70000.5"}, ConfigurationError)],
)
def test_market_and_fractional_won_orders_never_send(changes, error, live_safety):
    broker, calls = make({}, safety=live_safety)
    with pytest.raises(error):
        broker.place_order(order(**changes))
    assert calls == []


def test_etf_price_uses_the_venues_product_tick_rules(live_safety):
    broker, calls = make({ORDER + "cashBuy": [reply({"mkt_orr_no": 125})]}, safety=live_safety)
    result = broker.place_order(order(symbol="069500", price="10005"))
    assert result.id == "mkt:125"
    assert calls[0][3]["Input_0"]["orr_pr"] == 10005


def test_named_nxt_request_matches_capability_and_wire_venue(live_safety):
    broker, calls = make({ORDER + "cashBuy": [reply({"mkt_orr_no": 126})]}, safety=live_safety, venue="NXT")
    assert broker.capabilities().supports_nxt
    assert broker.place_order(order(venue="NXT")).id == "mkt:126"
    assert calls[0][3]["Input_0"]["rmt_mkt_cd"] == "NXT"


@pytest.mark.parametrize(
    "response,error",
    [
        ((401, {}, {}), BrokerRejectedError),
        (reply(code="IGW40043"), BrokerRejectedError),
        (reply(code=" IGW40043 "), BrokerRejectedError),
        (TimeoutError("secret-token"), TransportError),
        ((503, {}, {}), TransportError),
        (reply(), InvalidResponseError),
        (reply({"mkt_orr_no": 0}), InvalidResponseError),
    ],
)
def test_write_failure_is_once_and_never_leaks_credentials(response, error, live_safety):
    broker, calls = make({ORDER + "cashBuy": [response, reply({"mkt_orr_no": 999})]}, safety=live_safety)
    with pytest.raises(error) as raised:
        broker.place_order(order())
    assert [call[1] for call in calls] == [ORDER + "cashBuy"]
    assert all(secret not in str(raised.value) for secret in ("secret-key", "secret-value", "secret-token"))


def test_cancel_targets_only_one_market_order_and_uses_requested_quantity(live_safety):
    broker, calls = make({ORDER + "cancel": [reply({"mkt_orr_no": 888})]}, safety=live_safety)
    result = broker.cancel_order(cancel())
    assert result.id == "mkt:123" and result.status == "accepted"
    assert [call[1] for call in calls] == [ORDER + "cancel"]
    assert calls[0][3] == {
        "Input_0": {
            "act_no": "12345678901",
            "org_mkt_orr_no": 123,
            "all_pat_dit_cd": "2",
            "iem_cd": "005930",
            "cor_qty": 2,
        }
    }
    with pytest.raises(ConfigurationError):
        broker.cancel_order(cancel(order_id="itg:123", client_key="other-key"))
    assert len(calls) == 1


def test_daily_list_and_status_use_integrated_number_without_guessing_market_number():
    row = {
        "itg_orr_no": 456,
        "iem_cd": "005930",
        "sby_dit_cd_nm": "매수",
        "nmn_pr_tp_cd_nm": "보통가",
        "orr_qty": 2,
        "orr_pr": 70000,
        "tot_cns_qty": 1,
        "ny_cns_qty": 1,
    }
    page = reply([], message="조회완료", code="00000")
    page[1]["Output_1"] = [row]
    broker, calls = make({DAILY: [page, page]})
    (listed,) = broker.list_orders("005930")
    assert (listed.id, listed.status, listed.filled, listed.remaining) == ("itg:456", "partial", 1, 1)
    assert broker.status(listed).status == "partial"
    placed = Order(id="mkt:123", symbol="005930", status="accepted")
    assert broker.status(placed).status == "unknown"
    assert len(calls) == 2
    assert calls[0][3] == {
        "Input_0": {"orr_dt": "20260927", "act_no": "12345678901", "ost_cns_dit": "0", "orr_mkt_cd": "00"}
    }


def test_empty_daily_ledger_with_affirmative_envelope_is_empty():
    empty = reply(message="조회완료", code="00000")
    empty[1]["Output_1"] = []
    broker, _ = make({DAILY: [empty]})
    assert broker.list_orders() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"cor_can_dit_cd_nm": "취소"},
        {"orr_rjt_rsn_cd_nm": "잔고부족"},
        {"can_qty": 2},
        {"cor_qty": 2},
        {"orr_qty": 0, "ny_cns_qty": 0},
    ],
)
def test_daily_changed_rejected_or_zero_quantity_rows_stay_unknown(changes):
    row = {
        "itg_orr_no": 12345678901,
        "iem_cd": "005930",
        "orr_qty": 2,
        "orr_pr": 70000,
        "tot_cns_qty": 0,
        "ny_cns_qty": 2,
        **changes,
    }
    page = reply([], message="조회완료", code="00000")
    page[1]["Output_1"] = [row]
    broker, _ = make({DAILY: [page]})
    (listed,) = broker.list_orders()
    assert listed.id == "itg:12345678901" and listed.status == "unknown"


@pytest.mark.parametrize("reference_field", ["org_itg_orr_no", "mo_itg_orr_no"])
@pytest.mark.parametrize("reference_value", [456, "00000000456", " 456 "])
def test_daily_child_row_makes_parent_state_unknown(reference_field, reference_value):
    parent = {"itg_orr_no": 456, "iem_cd": "005930", "orr_qty": 2, "orr_pr": 70000, "tot_cns_qty": 0, "ny_cns_qty": 2}
    child = {**parent, "itg_orr_no": 789, reference_field: reference_value, "cor_can_dit_cd_nm": "취소"}
    page = reply([], message="조회완료", code="00000")
    page[1]["Output_1"] = [parent, child]
    broker, _ = make({DAILY: [page]})
    assert [item.status for item in broker.list_orders()] == ["unknown", "unknown"]


def test_cts_flag_normalizes_with_known_success_code():
    first = reply(message="조회완료", code="00000", headers={"cts_flag": " y ", "cts": " next "})
    first[1]["Output_1"] = []
    last = reply(
        {"dca": 0, "orr_pbl_amt": 0, "tot_eal_amt": 0},
        message="정상처리되었습니다",
        code="00000",
        headers={"cts_flag": " n "},
    )
    last[1]["Output_1"] = []
    broker, calls = make({"/krstock/inquiry/v1/balance": [first, last]})
    assert broker.get_balance().cash_available == 0
    assert calls[1][2]["cts"] == "next" and calls[1][2]["cts_flag"] == "Y"


def test_balance_padded_continuation_code_does_not_truncate_the_page_list():
    first = reply(message="조회완료", code=" 00165 ", headers={"cts": "next"})
    first[1]["Output_1"] = []
    last = reply({"dca": 500, "orr_pbl_amt": 500, "tot_eal_amt": 0}, message="조회완료", code="00166")
    last[1]["Output_1"] = []
    broker, calls = make({"/krstock/inquiry/v1/balance": [first, last]})
    assert broker.get_balance().cash_available == 500
    assert len(calls) == 2 and calls[1][2]["cts"] == "next"


US_ORDERS = "/gbstock/inquiry/v1/unexecuted"
US_POWER = "/gbstock/inquiry/v1/buyableAmount"


def us_row(**changes):
    return {
        "orr_no": 123,
        "iem_cd": "AAPL",
        "fc_sec_trd_nat_cd": "200",
        "oss_sby_dit_cd": "2",
        "ahi_nmn_pr_tp_cd": "00",
        "orr_qty": 2,
        "fc_orr_uit_pr": 230,
        "cns_qty": 1,
        "cns_pr": "229.75",
        "ny_cns_orr_qty": 1,
        **changes,
    }


def test_us_limit_cancel_status_and_fill_share_documented_order_number(live_safety):
    live_safety.valuation = lambda request: Decimal(1000)
    page = reply([us_row()])
    broker, calls = make(
        {
            "/gbstock/order/v1/buy": [reply({"orr_no": 123})],
            "/gbstock/order/v1/sell": [reply({"orr_no": 124})],
            "/gbstock/order/v1/cancel": [reply({"orr_no": 125})],
            US_ORDERS: [page],
        },
        market="US",
        venue="US",
        virtual=True,
        safety=live_safety,
    )
    request = OrderRequest(
        symbol="AAPL", market="US", side="buy", quantity=2, price=Decimal("230.25"), client_key="us-buy"
    )
    placed = broker.place_order(request)
    assert (placed.id, placed.market, placed.status) == ("us:123", "US", "accepted")
    broker.place_order(request.model_copy(update={"side": "sell", "client_key": "us-sell"}))
    broker.cancel_order(
        CancelRequest(order_id=placed.id, symbol="AAPL", market="US", quantity=1, client_key="us-cancel")
    )
    assert calls[0][3]["Input_0"] == {
        "act_no": "12345678901",
        "fc_sec_trd_nat_cd": "200",
        "iem_cd": "AAPL",
        "orr_qty": 2,
        "fc_orr_uit_pr": 230.25,
        "ahi_nmn_pr_tp_cd": "00",
        "wtm_cur_knd_cd": "1",
    }
    assert calls[2][3]["Input_0"]["org_orr_no"] == 123
    observed = broker.status(placed)
    assert (observed.status, observed.filled, observed.remaining, observed.fill_price) == (
        "partial",
        1,
        1,
        None,
    )
    assert calls[3][3]["Input_0"]["orr_dt"] == "20260926"  # New York, not Seoul date
    assert all(call[0] == NH_MOCK_BASE_URL for call in calls)


@pytest.mark.parametrize("integrated,wire", [(False, "4"), (True, "1")])
def test_us_buying_power_includes_integrated_only_for_explicit_existing_enrollment(integrated, wire):
    broker, calls = make(
        {
            US_POWER: [
                reply({"orr_pbl_qty": 3, "orr_pbl_amt": "500.25", "wtm_cur_cd": "USD"}),
                reply({"orr_pbl_amt": "500.25", "wtm_cur_cd": "USD"}),
            ]
        },
        market="US",
        venue="US",
        is_integrated=integrated,
    )
    assert broker.buyable_quantity("AAPL", price=Decimal("150.00")) == 3
    assert broker.buyable_amount("AAPL", price=Decimal("150.00")) == Decimal("500.25")
    assert calls[0][3]["Input_0"]["wtm_cur_knd_cd"] == wire
    assert [call[1] for call in calls] == [US_POWER, US_POWER]


@pytest.mark.parametrize(
    "row,state,price",
    [
        (us_row(cns_qty=2, ny_cns_orr_qty=0), "filled", None),
        (us_row(cns_qty=0, cns_pr=0, ny_cns_orr_qty=2), "open", None),
        (us_row(can_qty=1), "unknown", None),
        (us_row(rjt_rsn_cts="fixture-rejected"), "unknown", None),
        (us_row(orr_sts_nm="fixture-unverified"), "unknown", None),
    ],
)
def test_us_ledger_preserves_quantity_price_and_unknown_markers(row, state, price):
    broker, _ = make({US_ORDERS: [reply([row])]}, market="US", venue="US")
    (listed,) = broker.list_orders()
    assert (listed.status, listed.fill_price) == (state, price)


def test_official_kr_ledger_output_zero_preserves_execution_average():
    row = {
        "itg_orr_no": 456,
        "iem_cd": "005930",
        "orr_qty": 2,
        "orr_pr": 70000,
        "tot_cns_qty": 2,
        "ny_cns_qty": 0,
        "cns_avg_uit_pr": "69950",
    }
    broker, _ = make({DAILY: [reply([row])]})
    (listed,) = broker.list_orders()
    assert (listed.id, listed.status, listed.fill_price) == ("itg:456", "filled", Decimal("69950"))


@pytest.mark.parametrize("price", ["230.251", "230.0001"])
def test_us_excess_price_precision_is_rejected_before_transmission(price, live_safety):
    broker, calls = make({}, market="US", venue="US", safety=live_safety)
    with pytest.raises(ConfigurationError):
        broker.place_order(
            OrderRequest(symbol="AAPL", market="US", side="buy", quantity=1, price=price, client_key="precision")
        )
    assert calls == []


def test_us_write_timeout_does_not_replay(live_safety):
    live_safety.valuation = lambda request: Decimal(1000)
    broker, calls = make(
        {"/gbstock/order/v1/buy": [TimeoutError("fixture-private")]}, market="US", venue="US", safety=live_safety
    )
    with pytest.raises(TransportError):
        broker.place_order(
            OrderRequest(symbol="AAPL", market="US", side="buy", quantity=1, price=230, client_key="once")
        )
    assert len(calls) == 1


@pytest.mark.parametrize("currency", [None, "KRW", "EUR"])
def test_us_buying_power_rejects_wrong_or_missing_currency(currency):
    broker, _ = make({US_POWER: [reply({"orr_pbl_qty": 999, "wtm_cur_cd": currency})]}, market="US", venue="US")
    with pytest.raises(InvalidResponseError):
        broker.buyable_quantity("AAPL", price=Decimal(230))


def test_us_ledger_does_not_guess_a_foreign_country_or_parent_state():
    broker, _ = make({US_ORDERS: [reply([us_row(fc_sec_trd_nat_cd="070")])]}, market="US", venue="US")
    with pytest.raises(InvalidResponseError):
        broker.list_orders()
    broker, _ = make(
        {US_ORDERS: [reply([us_row(), us_row(orr_no=124, org_orr_no=123, can_qty=1)])]}, market="US", venue="US"
    )
    assert [row.status for row in broker.list_orders()] == ["unknown", "unknown"]


def test_us_balance_and_positions_keep_usd_and_broker_sellable_values():
    summary = {"fc_eal_amt": "450"}
    position = {
        "iem_cd": "AAPL",
        "fc_sec_trd_nat_cd": "200",
        "cur_cd": "USD",
        "cns_bse_bnc_qty": 2,
        "sll_pbl_qty1": 1,
        "fc_phs_uit_pr": 220,
        "fc_sec_end_pr": 225,
        "fc_eal_amt": 450,
    }
    balance = reply(summary)
    balance[1]["Output_1"] = [position]
    margin = reply([{"cur_cd": "USD", "fc_dca": 500, "fc_drn_pbl_amt": 400}])
    broker, _ = make(
        {"/gbstock/inquiry/v1/balance": [balance, balance], "/gbstock/inquiry/v1/margin": [margin]},
        market="US",
        venue="US",
    )
    funds = broker.get_balance()
    assert (funds.currency, funds.cash_available, funds.position_value) == ("USD", 400, 450)
    (holding,) = broker.get_positions()
    assert (holding.market, holding.quantity, holding.sellable, holding.average_price) == ("US", 2, 1, 220)
