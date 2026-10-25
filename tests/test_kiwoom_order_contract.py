"""Kiwoom order half, offline: limit buy/sell (kt10000/kt10001), single cancel (kt10003), open orders (ka10075) and
the day ledger (kt00007).

`tests/fixtures/kiwoom/{kt10000,kt10001,kt10003,ka10075,kt00007}.json` are the sample responses printed on the official
portal pages (read 2026-09-26 12:44 KST); kt10000's sample lacks a comma after `ord_no`, which is restored, and nothing
else is changed. They pin field names and formats only. Values derived from them are marked «synthetic». No live
response was captured, and what the spec does not say (error location, business error codes, the state a cancelled
order shows in kt00007) is refused or reported unknown rather than guessed.
"""

import inspect
import json
import urllib.error
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from pykbroker.brokers import kiwoom as module
from pykbroker.brokers.kiwoom import KIWOOM_API_BASE_URL, KIWOOM_MOCK_BASE_URL, Kiwoom
from pykbroker.exceptions import (
    BrokerError,
    BrokerRejectedError,
    ConfigurationError,
    DuplicateOrderError,
    InvalidResponseError,
    NotSupportedError,
    TransportError,
)
from pykbroker.factory import BROKERS, create_broker
from pykbroker.models import CancelRequest, Order, OrderRequest
from pykbroker.safety.rate_limit import RateLimiter

OFFICIAL = {
    path.stem: json.loads(path.read_text()) for path in (Path(__file__).parent / "fixtures" / "kiwoom").glob("*.json")
}
OPTIONS = {"token": "fixture-token", "app_key": "fixture-app-key"}
END = {"cont-yn": "N"}
NOW = datetime(2026, 9, 26, 16, 0, tzinfo=UTC)  # 2026-09-27 01:00 KST: the ledger date is the KST day, not the UTC one
ORDERS = "/api/dostk/ordr"
LEDGER = "acnt_ord_cntr_prps_dtl"


class FakeClient:
    def __init__(self, base_url, responses):
        self.base_url, self.calls, self.responses = base_url, [], {k: list(v) for k, v in responses.items()}

    def post(self, path, *, headers, json):
        self.calls.append({"path": path, "headers": headers, "json": json})
        response = self.responses[headers["api-id"]].pop(0)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, tuple) else (response, dict(END))


def make(responses=None, *, safety=None, options=OPTIONS, **kwargs):
    made = []

    def factory(*, base_url):
        made.append(FakeClient(base_url, {tr: page for tr, page in (responses or {}).items()}))
        return made[0]

    extra = {"safety": safety} if safety is not None else {}
    broker = create_broker(
        "kiwoom",
        account="fixture-account",
        client_factory=factory,
        client_options=options,
        codes={"exchange": "KRX", "query_type": "1"},
        rate_limiter=RateLimiter({tr: 1000 for tr in module.KIWOOM_TR_RATE_LIMITS}),
        clock=lambda: NOW,
        **extra,
        **kwargs,
    )
    return broker, made[0]


def buy(**changes):
    return OrderRequest(
        **{"symbol": "005930", "side": "buy", "quantity": 1, "price": "70000", "client_key": "k1", **changes}
    )


def cancel(**changes):
    return CancelRequest(**{"order_id": "0000140", "symbol": "005930", "quantity": 1, "client_key": "c1", **changes})


def held(**changes):
    return Order(**{"id": "0000050", "symbol": "069500", "status": "accepted", **changes})


def ledger(**changes):
    row = {**OFFICIAL["kt00007"][LEDGER][0], **changes}
    return {**OFFICIAL["kt00007"], LEDGER: [row]}


# --- kt10000 / kt10001: limit orders ---


def test_buy_request_and_the_official_sample(live_safety):
    broker, client = make({"kt10000": [OFFICIAL["kt10000"]]}, safety=live_safety)
    order = broker.place_order(buy())
    (call,) = client.calls
    assert call["path"] == ORDERS and client.base_url == KIWOOM_API_BASE_URL
    assert call["headers"] == {
        "content-type": "application/json; charset=utf-8",
        "authorization": "Bearer fixture-token",
        "api-id": "kt10000",
    }
    # trde_tp 0 is 보통 (the limit order); no ord_uv is left empty and cond_uv (stop price) is not sent
    assert call["json"] == {
        "dmst_stex_tp": "KRX",
        "stk_cd": "005930",
        "ord_qty": "1",
        "ord_uv": "70000",
        "trde_tp": "0",
    }
    assert order == Order(
        id="00024", symbol="005930", side="buy", order_type="limit", quantity=1, price=Decimal(70000),
        status="accepted", client_key="k1",
    )


def test_sell_goes_to_kt10001_and_a_virtual_account_to_the_mock_host(live_safety):
    broker, client = make({"kt10001": [OFFICIAL["kt10001"]]}, safety=live_safety, virtual=True)
    order = broker.place_order(buy(side="sell", quantity=3, price="2500", symbol="A005930"))
    (call,) = client.calls
    assert call["headers"]["api-id"] == "kt10001" and client.base_url == KIWOOM_MOCK_BASE_URL
    assert call["json"] == {"dmst_stex_tp": "KRX", "stk_cd": "005930", "ord_qty": "3", "ord_uv": "2500", "trde_tp": "0"}
    assert (order.id, order.side, order.status, order.virtual) == ("0000138", "sell", "accepted", True)


def test_orders_the_account_mode_or_market_cannot_take_are_refused_before_any_request(live_safety):
    # a mock (virtual) account trades KRX only (official mock info); NXT/SOR on the real host are covered in
    # test_kiwoom_order_kinds.py. US takes plain day limit orders only.
    broker, client = make({}, safety=live_safety, virtual=True)
    for request in (
        buy(venue="NXT"),
        buy(venue="SOR"),
        buy(market="US"),
        buy(time_in_force="ioc", venue="KRX", order_type="limit", market="US"),
    ):
        with pytest.raises(NotSupportedError):
            broker.place_order(request)
    with pytest.raises(ConfigurationError):
        broker.place_order(buy(price="70000.5"))
    assert client.calls == []


@pytest.mark.parametrize(
    "failure,expected",
    [
        (OSError("reset"), TransportError),
        (TimeoutError("read timed out"), TransportError),
        (urllib.error.HTTPError("https://x.invalid", 500, "fixture-token", {}, None), TransportError),
        (ValueError("not json"), InvalidResponseError),
        (("not a tuple", {}), InvalidResponseError),
    ],
)
def test_a_failed_or_unreadable_send_is_unknown_and_the_post_goes_out_once(live_safety, failure, expected):
    # the second scripted answer would have been a success: a retry would have turned an unknown into an order
    broker, client = make({"kt10000": [failure, OFFICIAL["kt10000"]]}, safety=live_safety)
    with pytest.raises(expected) as raised:
        broker.place_order(buy())
    assert len(client.calls) == 1
    assert "fixture-token" not in str(raised.value) and not isinstance(raised.value, BrokerRejectedError)


@pytest.mark.parametrize("code", [1, 8005, 1700, -1, 1998, 2000])
def test_an_integer_return_code_but_zero_and_1999_is_a_refusal(live_safety, code):
    body = {**OFFICIAL["kt10000"], "return_code": code, "return_msg": "secret detail"}
    broker, client = make({"kt10000": [body]}, safety=live_safety)
    with pytest.raises(BrokerRejectedError) as raised:
        broker.place_order(buy())
    assert len(client.calls) == 1 and "secret detail" not in str(raised.value)


def test_1999_unexpected_error_on_a_write_is_unknown_not_a_refusal(live_safety):
    # 1999 «예기치 못한 에러» may come after the order was taken: not «safe to resubmit». One POST, never a retry.
    body = {**OFFICIAL["kt10000"], "return_code": 1999, "return_msg": "secret detail"}
    broker, client = make({"kt10000": [body, OFFICIAL["kt10000"]]}, safety=live_safety)
    with pytest.raises(InvalidResponseError) as raised:
        broker.place_order(buy())
    assert not isinstance(raised.value, BrokerRejectedError) and len(client.calls) == 1
    assert "secret detail" not in str(raised.value)
    broker, client = make(
        {"kt10003": [{**OFFICIAL["kt10003"], "return_code": 1999}, OFFICIAL["kt10003"]]}, safety=live_safety
    )
    with pytest.raises(InvalidResponseError) as raised:
        broker.cancel_order(cancel())
    assert not isinstance(raised.value, BrokerRejectedError) and len(client.calls) == 1


@pytest.mark.parametrize("code", ["0", "8005", True, False, None, 0.0, [0]])
def test_a_code_that_is_not_an_integer_is_unknown_never_a_refusal(live_safety, code):
    # a refusal frees the caller to resubmit; a body that cannot be read as either answer must not
    broker, client = make({"kt10000": [{**OFFICIAL["kt10000"], "return_code": code}]}, safety=live_safety)
    with pytest.raises(InvalidResponseError) as raised:
        broker.place_order(buy())
    assert not isinstance(raised.value, BrokerRejectedError) and len(client.calls) == 1


def test_a_body_without_return_code_is_unknown(live_safety):
    body = {k: v for k, v in OFFICIAL["kt10000"].items() if k != "return_code"}
    with pytest.raises(InvalidResponseError):
        make({"kt10000": [body]}, safety=live_safety)[0].place_order(buy())


@pytest.mark.parametrize("ord_no", [None, "", "  ", "0000000", "12345678", "12a", 74, True])
def test_a_success_without_a_usable_order_number_says_to_reconcile(live_safety, ord_no):
    body = {**OFFICIAL["kt10000"], "ord_no": ord_no}
    with pytest.raises(InvalidResponseError, match="reconcile"):
        make({"kt10000": [body]}, safety=live_safety)[0].place_order(buy())


def test_a_success_without_any_order_number_says_to_reconcile(live_safety):
    body = {k: v for k, v in OFFICIAL["kt10000"].items() if k != "ord_no"}
    with pytest.raises(InvalidResponseError, match="reconcile"):
        make({"kt10000": [body]}, safety=live_safety)[0].place_order(buy())


def test_an_acknowledgement_from_another_exchange_is_not_an_answer(live_safety):
    body = {**OFFICIAL["kt10001"], "dmst_stex_tp": "NXT"}
    with pytest.raises(InvalidResponseError, match="reconcile"):
        make({"kt10001": [body]}, safety=live_safety)[0].place_order(buy(side="sell"))


def test_a_repeated_client_key_is_one_post(live_safety):
    broker, client = make({"kt10000": [OFFICIAL["kt10000"], OFFICIAL["kt10000"]]}, safety=live_safety)
    broker.place_order(buy())
    with pytest.raises(DuplicateOrderError):
        broker.place_order(buy())
    assert len(client.calls) == 1


def test_the_default_safety_never_reaches_the_wire():
    broker, client = make({"kt10000": [OFFICIAL["kt10000"]]})
    with pytest.raises(BrokerError):
        broker.place_order(buy())
    assert client.calls == []


def test_an_expired_stored_token_is_not_sent_with_an_order(live_safety):
    options = {**OPTIONS, "expires_dt": "20260926000000"}
    broker, client = make({"kt10000": [OFFICIAL["kt10000"]]}, safety=live_safety, options=options)
    with pytest.raises(ConfigurationError):
        broker.place_order(buy())
    assert client.calls == []


def test_the_limiter_is_awaited_before_the_order_post(live_safety):
    events = []

    class Limiter:
        def wait(self, tr):
            events.append(("wait", tr))

    broker, client = make({"kt10000": [OFFICIAL["kt10000"]]}, safety=live_safety)
    broker._limiter = Limiter()
    client.post = lambda path, *, headers, json: (
        events.append(("post", headers["api-id"])) or (OFFICIAL["kt10000"], END)
    )
    broker.place_order(buy())
    assert events == [("wait", "kt10000"), ("post", "kt10000")]


# --- kt10003: single cancel ---


def test_cancel_request_and_the_official_sample(live_safety):
    broker, client = make({"kt10003": [OFFICIAL["kt10003"]]}, safety=live_safety)
    result = broker.cancel_order(cancel())
    (call,) = client.calls
    assert call["path"] == ORDERS and call["headers"]["api-id"] == "kt10003"
    assert call["json"] == {"dmst_stex_tp": "KRX", "orig_ord_no": "0000140", "stk_cd": "005930", "cncl_qty": "1"}
    # the order stays the one that was cancelled; the cancel's own number (0000141) is not its identity
    assert result == Order(id="0000140", symbol="005930", status="accepted", venue=None, client_key="c1")


def test_cancel_quantity_is_explicit_never_the_remainder_shorthand(live_safety):
    broker, client = make({"kt10003": [{**OFFICIAL["kt10003"], "cncl_qty": "000000000003"}]}, safety=live_safety)
    broker.cancel_order(cancel(quantity=3))
    assert client.calls[0]["json"]["cncl_qty"] == "3"  # "0" would cancel everything left
    with pytest.raises(Exception):
        cancel(quantity=0)


@pytest.mark.parametrize("order_id", ["12345678", "abc", "00 1", "-1", "１２３", "0000000", "0"])
def test_cancel_refuses_an_order_number_that_is_not_the_documented_form(live_safety, order_id):
    broker, client = make({"kt10003": [OFFICIAL["kt10003"]]}, safety=live_safety)
    with pytest.raises(ConfigurationError):
        broker.cancel_order(cancel(order_id=order_id))
    assert client.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {**OFFICIAL["kt10003"], "cncl_qty": "000000000002"},
        {**OFFICIAL["kt10003"], "cncl_qty": "000000000000"},
        {**OFFICIAL["kt10003"], "ord_no": "0000000"},
        {k: v for k, v in OFFICIAL["kt10003"].items() if k != "ord_no"},
        {k: v for k, v in OFFICIAL["kt10003"].items() if k != "cncl_qty"},
        {**OFFICIAL["kt10003"], "return_code": "0"},
    ],
)
def test_a_cancel_answer_that_cannot_be_read_is_unknown(live_safety, body):
    broker, client = make({"kt10003": [body, OFFICIAL["kt10003"]]}, safety=live_safety)
    with pytest.raises(InvalidResponseError) as raised:
        broker.cancel_order(cancel())
    assert not isinstance(raised.value, BrokerRejectedError) and len(client.calls) == 1


def test_cancel_refusal_and_transport_failure_are_classified_once(live_safety):
    broker, client = make(
        {"kt10003": [{**OFFICIAL["kt10003"], "return_code": 1, "return_msg": "secret detail"}]}, safety=live_safety
    )
    with pytest.raises(BrokerRejectedError) as raised:
        broker.cancel_order(cancel())
    assert "secret detail" not in str(raised.value) and len(client.calls) == 1
    broker, client = make({"kt10003": [OSError("reset"), OFFICIAL["kt10003"]]}, safety=live_safety)
    with pytest.raises(TransportError):
        broker.cancel_order(cancel(client_key="c2"))
    assert len(client.calls) == 1


def test_us_amend_and_unimplemented_cash_query_stay_unsupported():
    source = inspect.getsource(module)
    code = "\n".join(line.split("#")[0] for line in source.splitlines())
    assert "ust20002" not in code and "kt00010" not in code
    assert not hasattr(Kiwoom, "modify_order") and hasattr(Kiwoom, "amend_order")
    assert BROKERS["kiwoom"] is Kiwoom


# --- ka10075: open orders ---


@pytest.mark.parametrize("price", ["70000", "+70000", "-70000"])
def test_open_order_signed_price_contract(price):
    row = {**OFFICIAL["ka10075"]["oso"][0], "ord_pric": price}
    broker, client = make({"ka10075": [{**OFFICIAL["ka10075"], "oso": [row]}]})
    assert broker.list_orders()[0].price == Decimal("70000")
    assert [call["headers"]["api-id"] for call in client.calls] == ["ka10075"]


def test_open_orders_request_and_the_official_sample():
    broker, client = make({"ka10075": [OFFICIAL["ka10075"]]})
    orders = broker.list_orders()
    (call,) = client.calls
    assert call["path"] == "/api/dostk/acnt" and call["headers"]["api-id"] == "ka10075"
    assert call["json"] == {"all_stk_tp": "0", "trde_tp": "0", "stex_tp": "0"}  # 전체·전체·통합 (mock: KRX only)
    # +매수 (the sign only colours the label), 시장가 has no price, nothing is filled yet
    assert orders == [
        Order(
            id="0000069",
            symbol="005930",
            side="buy",
            order_type="market",
            quantity=1,
            price=None,
            filled=0,
            remaining=1,
            status="open",
            venue="KRX",
        )
    ]


def test_open_orders_for_one_symbol_name_it_and_filter_on_it():
    other = {**OFFICIAL["ka10075"]["oso"][0], "ord_no": "0000070", "stk_cd": "000660"}
    page = {**OFFICIAL["ka10075"], "oso": [OFFICIAL["ka10075"]["oso"][0], other]}
    broker, client = make({"ka10075": [page]})
    assert [o.id for o in broker.list_orders("A005930")] == ["0000069"]
    assert client.calls[0]["json"] == {"all_stk_tp": "1", "trde_tp": "0", "stex_tp": "0", "stk_cd": "005930"}


@pytest.mark.parametrize(
    "changes,status,filled,remaining",
    [
        ({"cntr_qty": "1", "oso_qty": "2", "ord_qty": "3"}, "partial", 1, 2),
        ({"cntr_qty": "0", "oso_qty": "3", "ord_qty": "3"}, "open", 0, 3),
        ({"cntr_qty": "3", "oso_qty": "0", "ord_qty": "3"}, "unknown", 3, 0),  # not «unfilled» any more: no claim
    ],
)
def test_open_order_status_comes_from_the_quantity_fields_only(changes, status, filled, remaining):
    row = {**OFFICIAL["ka10075"]["oso"][0], "ord_stt": "접수", "trde_tp": "보통", "ord_pric": "+70000", **changes}
    (order,) = make({"ka10075": [{**OFFICIAL["ka10075"], "oso": [row]}]})[0].list_orders()
    assert (order.status, order.filled, order.remaining) == (status, filled, remaining)
    assert order.order_type == "limit" and order.price == Decimal(70000)


@pytest.mark.parametrize(
    "changes",
    [
        {"ord_no": "0000000"},
        {"ord_no": ""},
        {"oso_qty": "x"},
        {"cntr_qty": "-1"},
        {"ord_pric": "--70000"},
        {"stk_cd": "AAPL"},
    ],
)
def test_a_malformed_open_order_row_fails_the_whole_read(changes):
    row = {**OFFICIAL["ka10075"]["oso"][0], **changes}
    with pytest.raises(BrokerError):
        make({"ka10075": [{**OFFICIAL["ka10075"], "oso": [row]}]})[0].list_orders()


def test_an_unnamed_type_or_side_stays_unknown():
    row = {**OFFICIAL["ka10075"]["oso"][0], "trde_tp": "스톱지정가", "io_tp_nm": "-매도정정"}
    (order,) = make({"ka10075": [{**OFFICIAL["ka10075"], "oso": [row]}]})[0].list_orders()
    assert order.order_type is None and order.side is None


def test_open_order_pages_are_joined_and_a_list_that_may_be_cut_is_refused():
    second = {**OFFICIAL["ka10075"]["oso"][0], "ord_no": "0000071"}
    pages = [
        (OFFICIAL["ka10075"], {"cont-yn": "Y", "next-key": "K1"}),
        ({**OFFICIAL["ka10075"], "oso": [second]}, dict(END)),
    ]
    broker, client = make({"ka10075": pages})
    assert [o.id for o in broker.list_orders()] == ["0000069", "0000071"]
    assert client.calls[1]["headers"]["cont-yn"] == "Y" and client.calls[1]["headers"]["next-key"] == "K1"
    with pytest.raises(InvalidResponseError):
        make({"ka10075": [(OFFICIAL["ka10075"], {})]})[0].list_orders()


def test_open_orders_carry_the_account_mode_and_a_refusal_is_a_refusal():
    (order,) = make({"ka10075": [OFFICIAL["ka10075"]]}, virtual=True)[0].list_orders()
    assert order.virtual is True
    with pytest.raises(BrokerRejectedError):
        make({"ka10075": [{**OFFICIAL["ka10075"], "return_code": 8104}]})[0].list_orders()


# --- kt00007: state and fills ---


def test_status_request_and_the_official_sample():
    broker, client = make({"kt00007": [OFFICIAL["kt00007"]]})
    result = broker.status(held())
    (call,) = client.calls
    assert call["path"] == "/api/dostk/acnt" and call["headers"]["api-id"] == "kt00007"
    # 주문순 · 주식 · 매매구분 전체 · KRX, for today's KST date (the UTC date is still the 26th)
    assert call["json"] == {
        "ord_dt": "20260927",
        "qry_tp": "1",
        "stk_bond_tp": "1",
        "sell_tp": "0",
        "stk_cd": "069500",
        "dmst_stex_tp": "KRX",
    }
    # the official sample: 1 ordered, 1 executed, nothing left (its A069500 prefix is the venue's, not the symbol's)
    assert (result.status, result.quantity, result.filled, result.remaining) == ("filled", 1, 1, 0)
    assert result.id == "0000050" and result.symbol == "069500"


@pytest.mark.parametrize(
    "changes,status,filled,remaining",
    [
        ({"cntr_qty": "0000000000", "ord_remnq": "0000000001"}, "open", 0, 1),
        ({"ord_qty": "0000000003", "cntr_qty": "0000000001", "ord_remnq": "0000000002"}, "partial", 1, 2),
        ({"ord_qty": "0000000003", "cntr_qty": "0000000003", "ord_remnq": "0000000000"}, "filled", 3, 0),
        # the numbers do not add up, or nothing was executed and nothing is left: no claim either way
        ({"ord_qty": "0000000003", "cntr_qty": "0000000001", "ord_remnq": "0000000001"}, "unknown", 1, 1),
        ({"cntr_qty": "0000000000", "ord_remnq": "0000000000"}, "unknown", 0, 0),
    ],
)
def test_status_is_decided_by_the_ledger_quantities_only(changes, status, filled, remaining):
    result = make({"kt00007": [ledger(**changes)]})[0].status(held())
    assert (result.status, result.filled, result.remaining) == (status, filled, remaining)


@pytest.mark.parametrize("change", ["취소확인", "정정확인"])
def test_a_cancel_or_amend_row_pointing_at_the_order_leaves_it_unknown_unless_fully_executed(change):
    cancel_row = {**OFFICIAL["kt00007"][LEDGER][0], "ord_no": "0000051", "ori_ord": "0000050", "mdfy_cncl": change}
    open_row = {**OFFICIAL["kt00007"][LEDGER][0], "cntr_qty": "0000000000", "ord_remnq": "0000000001"}
    page = {**OFFICIAL["kt00007"], LEDGER: [open_row, cancel_row]}
    # the spec does not say what the original row shows once cancelled, so «open» would be a guess
    assert make({"kt00007": [page]})[0].status(held()).status == "unknown"
    page = {**OFFICIAL["kt00007"], LEDGER: [OFFICIAL["kt00007"][LEDGER][0], cancel_row]}
    assert make({"kt00007": [page]})[0].status(held()).status == "filled"


@pytest.mark.parametrize(
    "page",
    [
        {**OFFICIAL["kt00007"], LEDGER: []},
        ledger(ord_no="0000051"),
        ledger(stk_cd="A005930"),
        {**OFFICIAL["kt00007"], LEDGER: [OFFICIAL["kt00007"][LEDGER][0]] * 2},
    ],
)
def test_an_order_the_ledger_does_not_show_exactly_once_for_that_symbol_is_unknown(page):
    result = make({"kt00007": [page]})[0].status(held(quantity=1, status="accepted"))
    assert result.status == "unknown" and (result.filled, result.remaining) == (None, None)


def test_status_is_never_cancelled_and_never_guessed_from_an_unreadable_row():
    for status in (make({"kt00007": [ledger(mdfy_cncl="취소확인")]})[0].status(held()),):
        assert status.status != "cancelled"
    for bad in ({"ord_qty": "x"}, {"ord_remnq": "-0000000001"}, {"cntr_qty": ""}):
        with pytest.raises(InvalidResponseError):
            make({"kt00007": [ledger(**bad)]})[0].status(held())
    with pytest.raises(BrokerRejectedError):
        make({"kt00007": [{**OFFICIAL["kt00007"], "return_code": 8104}]})[0].status(held())


def test_status_reads_every_page_and_refuses_a_ledger_that_may_be_cut():
    other = {**OFFICIAL["kt00007"][LEDGER][0], "ord_no": "0000049"}
    pages = [
        ({**OFFICIAL["kt00007"], LEDGER: [other]}, {"cont-yn": "Y", "next-key": "K1"}),
        (OFFICIAL["kt00007"], dict(END)),
    ]
    assert make({"kt00007": pages})[0].status(held()).status == "filled"
    with pytest.raises(InvalidResponseError):
        make({"kt00007": [(OFFICIAL["kt00007"], {})]})[0].status(held())


def test_status_refuses_another_market_and_a_closed_broker():
    broker, client = make({"kt00007": [OFFICIAL["kt00007"]]})
    with pytest.raises(NotSupportedError):
        broker.status(held(market="US"))
    broker.close()
    with pytest.raises(ConfigurationError):
        broker.status(held())
    assert client.calls == []


# US REST fields: Kiwoom-Securities/Kiwoom-REST-API kiwoom/_data/kiwoom_api_spec.json,
# read 2026-10-04. Values below are synthetic; this is never a live account response.
US_ROW = {
    "ord_no": "000000282",
    "stk_cd": "NVDA",
    "ord_cntr_tp": "10",
    "crnc_code": "USD",
    "slby_tp": "2",
    "frgn_trde_tp": "00",
    "ord_qty": "5",
    "cntr_qty": "0",
    "ord_remnq": "5",
    "mdfy_qty": "0",
    "cncl_qty": "0",
    "ord_uv": "200.0000",
    "cntr_uv": "0.0000",
}


def us_make(responses=None, **kwargs):
    # Synthetic offline safety valuation only; the production factory owns currency policy.
    if kwargs.get("safety") is not None:
        kwargs["safety"].valuation = lambda request: request.price * request.quantity
    return make(responses, market="US", venue="NASDAQ", **kwargs)


@pytest.mark.parametrize("side,tr", [("buy", "ust20000"), ("sell", "ust20001")])
def test_us_limit_order_uses_official_tr_and_decimal_price_once(live_safety, side, tr):
    broker, client = us_make({tr: [{"return_code": 0, "ord_no": "000000282"}]}, safety=live_safety)
    order = broker.place_order(buy(symbol="NVDA", market="US", venue="NASDAQ", side=side, price="200.0001"))
    assert len(client.calls) == 1
    assert client.calls[0]["headers"]["api-id"] == tr
    assert client.calls[0]["path"] == "/api/us/ordr"
    assert client.calls[0]["json"] == {
        "stex_tp": "ND",
        "stk_cd": "NVDA",
        "ord_qty": "1",
        "ord_uv": "200.0001",
        "trde_tp": "00",
    }
    assert (order.id, order.status, order.market, order.side) == ("000000282", "accepted", "US", side)


@pytest.mark.parametrize("failure", [TimeoutError("uncertain"), {"return_code": 1999}, {"return_code": "0"}])
def test_us_uncertain_send_never_retries(live_safety, failure):
    broker, client = us_make({"ust20000": [failure, {"return_code": 0, "ord_no": "000000282"}]}, safety=live_safety)
    with pytest.raises((InvalidResponseError, TransportError)):
        broker.place_order(buy(symbol="NVDA", market="US", venue="NASDAQ", price="200"))
    assert len(client.calls) == 1


def test_us_partial_cancel_is_refused_before_write(live_safety):
    broker, client = us_make({"ust21050": [{"return_code": 0, "result_list": [US_ROW]}]}, safety=live_safety)
    with pytest.raises(NotSupportedError, match="full confirmed"):
        broker.cancel_order(cancel(symbol="NVDA", market="US", venue="NASDAQ", order_id="000000282", quantity=1))
    assert [c["headers"]["api-id"] for c in client.calls] == ["ust21050"]


def test_us_full_cancel_verifies_remainder_and_never_names_a_cancel_ack_as_final(live_safety):
    broker, client = us_make(
        {
            "ust21050": [{"return_code": 0, "result_list": [US_ROW]}],
            "ust20003": [{"return_code": 0, "ord_no": "000000285", "cncl_ord_qty": "5"}],
        },
        safety=live_safety,
        virtual=True,
    )
    order = broker.cancel_order(cancel(symbol="NVDA", market="US", venue="NASDAQ", order_id="000000282", quantity=5))
    assert client.base_url == KIWOOM_MOCK_BASE_URL
    assert client.calls[-1]["json"] == {"stex_tp": "ND", "stk_cd": "NVDA", "orig_ord_no": "000000282"}
    assert (order.id, order.status, order.market) == ("000000282", "accepted", "US")


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"cntr_qty": "5", "ord_remnq": "0"}, "filled"),
        ({"cntr_qty": "2", "ord_remnq": "3"}, "partial"),
        ({"cntr_qty": "2", "ord_remnq": "4"}, "unknown"),
        ({"cncl_qty": "5", "ord_remnq": "0"}, "unknown"),
    ],
)
def test_us_ledger_quantities_determine_status_without_fabricated_average(changes, status):
    row = {**US_ROW, **changes, "cntr_uv": "199.7700"}
    broker, _ = us_make({"ust21150": [{"return_code": 0, "result_list": [row]}]})
    order = broker.status(Order(id="000000282", symbol="NVDA", market="US"))
    assert order.status == status
    assert order.fill_price is None


def test_us_absent_or_ambiguous_ledger_row_stays_unknown():
    for rows in ([], [US_ROW, US_ROW]):
        broker, _ = us_make({"ust21150": [{"return_code": 0, "result_list": rows}]})
        assert broker.status(Order(id="000000282", symbol="NVDA", market="US")).status == "unknown"


def test_one_share_fill_preserves_the_broker_execution_unit_price_in_both_markets():
    broker, _ = make({"kt00007": [ledger(ord_qty="1", cntr_qty="1", ord_remnq="0", cntr_uv="70050")]})
    assert broker.status(held()).fill_price == Decimal("70050")
    row = {**US_ROW, "ord_qty": "1", "cntr_qty": "1", "ord_remnq": "0", "cntr_uv": "199.77"}
    broker, _ = us_make({"ust21150": [{"return_code": 0, "result_list": [row]}]})
    assert broker.status(Order(id="000000282", symbol="NVDA", market="US")).fill_price == Decimal("199.77")


PINNED = {
    path.stem: json.loads(path.read_text())
    for path in (Path(__file__).parent / "fixtures" / "kiwoom" / "official").glob("*.json")
    if path.stem != "error_codes"
}


@pytest.mark.parametrize(
    "filled,total,expected",
    [(0, "0", None), (2, "140100", Decimal("70050")), (2, "0", None), (2, "", None), (2, None, None)],
)
def test_pinned_open_order_average_uses_execution_total_only(filled, total, expected):
    # Official sample has zero fills. The multi-fill values are derived from its field layout.
    body = PINNED["ka10075"]["response"]
    row = {**body["oso"][0], "cntr_qty": str(filled), "cntr_tot_amt": total, "ord_qty": "3", "oso_qty": str(3 - filled)}
    (order,) = make({"ka10075": [{**body, "oso": [row]}]})[0].list_orders()
    assert order.fill_price == expected


@pytest.mark.parametrize(
    "filled,cancelled,remaining,status",
    [
        (0, 3, 0, "cancelled"),
        (1, 2, 0, "cancelled"),
        (0, 2, 1, "open"),
        (1, 1, 1, "partial"),
        (1, 1, 0, "unknown"),
        (0, 4, 0, "unknown"),
    ],
)
def test_pinned_cancel_state_requires_conserved_confirmed_quantities(filled, cancelled, remaining, status):
    # kt00007 has no cancel response example: these are derived confirmed-cancel rows.
    body = PINNED["kt00007"]["response"]
    base = body[LEDGER][0]
    original = {**base, "ord_qty": "0000000003", "cntr_qty": str(filled), "ord_remnq": str(remaining)}
    change = {
        **base,
        "ord_no": "0000051",
        "ori_ord": base["ord_no"],
        "mdfy_cncl": "취소확인",
        "cnfm_qty": str(cancelled),
        "ord_qty": "99",
    }
    result = make({"kt00007": [{**body, LEDGER: [original, change]}]})[0].status(held())
    assert (result.status, result.quantity, result.filled, result.remaining) == (status, 3, filled, remaining)


@pytest.mark.parametrize(
    "change",
    [
        {"mdfy_cncl": "취소접수"},
        {"mdfy_cncl": "취소"},
        {"mdfy_cncl": "취소거부"},
        {"mdfy_cncl": "정정"},
        {"mdfy_cncl": "알 수 없는 값"},
        {"mdfy_cncl": "취소확인", "cnfm_qty": "1"},
        {"mdfy_cncl": "취소확인", "dmst_stex_tp": "NXT"},
        {"mdfy_cncl": "정정확인"},
    ],
)
def test_pinned_fully_filled_order_survives_late_cancel_or_amend_rows(change):
    # Derived late changes: the official ledger sample has one share fully executed.
    body = PINNED["kt00007"]["response"]
    original = body[LEDGER][0]
    late = {**original, "ord_no": "0000051", "ori_ord": original["ord_no"], **change}
    result = make({"kt00007": [{**body, LEDGER: [original, late]}]})[0].status(held())
    assert (result.status, result.quantity, result.filled, result.remaining) == ("filled", 1, 1, 0)


@pytest.mark.parametrize(
    "change",
    [
        {"mdfy_cncl": "취소접수"},
        {"mdfy_cncl": "알 수 없는 값"},
        {"mdfy_cncl": "정정확인"},
        {"stk_cd": "A005930"},
        {"dmst_stex_tp": "NXT"},
        {"cnfm_qty": "0"},
    ],
)
def test_pinned_unconfirmed_or_other_instrument_cancel_stays_unknown(change):
    body = PINNED["kt00007"]["response"]
    base = body[LEDGER][0]
    original = {**base, "cntr_qty": "0", "ord_remnq": "0"}
    cancel_row = {
        **base,
        "ord_no": "0000051",
        "ori_ord": base["ord_no"],
        "mdfy_cncl": "취소확인",
        "cnfm_qty": "1",
        **change,
    }
    assert make({"kt00007": [{**body, LEDGER: [original, cancel_row]}]})[0].status(held()).status == "unknown"


def test_pinned_duplicate_cancel_rows_do_not_double_count():
    body = PINNED["kt00007"]["response"]
    base = body[LEDGER][0]
    original = {**base, "ord_qty": "2", "cntr_qty": "0", "ord_remnq": "0"}
    cancel_row = {**base, "ord_no": "0000051", "ori_ord": base["ord_no"], "mdfy_cncl": "취소확인", "cnfm_qty": "1"}
    assert (
        make({"kt00007": [{**body, LEDGER: [original, cancel_row, cancel_row]}]})[0].status(held()).status == "unknown"
    )
