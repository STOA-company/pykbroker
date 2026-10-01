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


def make(script, *, safety=None, virtual=False, venue="KRX"):
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
        clock=lambda: NOW,
        rate_limiter=RateLimiter({"read": 10000}),
        safety=safety,
        virtual=virtual,
        venue=venue,
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
    page = reply([], message="조회완료", code=None)
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
    broker, _ = make({DAILY: [reply(message="조회완료", code=None)]})
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
    page = reply([], message="조회완료", code=None)
    page[1]["Output_1"] = [row]
    broker, _ = make({DAILY: [page]})
    (listed,) = broker.list_orders()
    assert listed.id == "itg:12345678901" and listed.status == "unknown"


@pytest.mark.parametrize("reference_field", ["org_itg_orr_no", "mo_itg_orr_no"])
@pytest.mark.parametrize("reference_value", [456, "00000000456", " 456 "])
def test_daily_child_row_makes_parent_state_unknown(reference_field, reference_value):
    parent = {"itg_orr_no": 456, "iem_cd": "005930", "orr_qty": 2, "orr_pr": 70000, "tot_cns_qty": 0, "ny_cns_qty": 2}
    child = {**parent, "itg_orr_no": 789, reference_field: reference_value, "cor_can_dit_cd_nm": "취소"}
    page = reply([], message="조회완료", code=None)
    page[1]["Output_1"] = [parent, child]
    broker, _ = make({DAILY: [page]})
    assert [item.status for item in broker.list_orders()] == ["unknown", "unknown"]


def test_cts_flag_normalizes_and_schema_envelope_without_rsp_cd_is_accepted():
    first = reply(message="조회완료", code=None, headers={"cts_flag": " y ", "cts": " next "})
    first[1]["Output_1"] = []
    last = reply(
        {"dca": 0, "orr_pbl_amt": 0, "tot_eal_amt": 0},
        message="정상처리되었습니다",
        code=None,
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
