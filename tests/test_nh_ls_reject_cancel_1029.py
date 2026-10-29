"""Regression for trader #676: no invented rejection or cancellation terminal states."""

from datetime import date
from decimal import Decimal

import pytest

from pykbroker.exceptions import BrokerRejectedError, DuplicateOrderError, InvalidResponseError
from tests.test_ls_contract import buy, cancel, make_mock, unlocked  # noqa: F401
from tests.test_ls_contract import make as ls_make
from tests.test_nh_ls_definite_rejects_1028 import _cancel_row, _ls_row, _t0425
from tests.test_nh_order_contract import cancel as nh_cancel
from tests.test_nh_order_contract import make, order, reply

# Independently listed from NH's official /testbed-howto error table (not imported from implementation).
NH_REJECT_CODES = ("IGW40012", "IGW40018", "IGW40050", "IGW40051", "IGW40301", "IGW40302", "IGW40401")
UNKNOWN_CODES = (None, "", " ", "UNREGISTERED_97", "90001", "03001", "IGW40013", "IGW50007", "IGW50020")


def _nh_request(market, operation):
    if market == "KR":
        return nh_cancel() if operation == "cancel" else order(side=operation)
    if operation == "cancel":
        return nh_cancel(order_id="us:123", symbol="AAPL", market="US", quantity=1)
    return order(symbol="AAPL", market="US", side=operation, price="100")


def _nh_path(market, operation):
    endpoint = {"buy": "cashBuy", "sell": "cashSell", "cancel": "cancel"}[operation] if market == "KR" else operation
    return ("/krstock" if market == "KR" else "/gbstock") + "/order/v1/" + endpoint


@pytest.mark.parametrize("market", ["KR", "US"])
@pytest.mark.parametrize("operation", ["buy", "sell", "cancel"])
@pytest.mark.parametrize("code", UNKNOWN_CODES)
def test_nh_unknown_codes_keep_claim_and_never_resend(market, operation, code, live_safety):
    path = _nh_path(market, operation)
    live_safety.valuation = lambda request: Decimal(1000)
    broker, calls = make({path: [reply(code=code, message="응답을 수신하지 못했습니다.")]},
                         market=market, venue="US" if market == "US" else "KRX",
                         safety=live_safety, definite_rejects=True)
    request = _nh_request(market, operation)
    call = broker.cancel_order if operation == "cancel" else broker.place_order
    with pytest.raises(InvalidResponseError):
        call(request)
    with pytest.raises(DuplicateOrderError):
        call(request)
    assert len(calls) == 1


@pytest.mark.parametrize("market", ["KR", "US"])
@pytest.mark.parametrize("operation", ["buy", "sell", "cancel"])
@pytest.mark.parametrize("code", NH_REJECT_CODES)
@pytest.mark.parametrize("with_number", [False, True])
def test_nh_only_documented_preacceptance_codes_reject(market, operation, code, with_number, live_safety):
    path = _nh_path(market, operation)
    output = {"orr_no" if market == "US" else "mkt_orr_no": 123} if with_number else None
    live_safety.valuation = lambda request: Decimal(1000)
    broker, calls = make({path: [reply(output, code=code, message="요청 형식이 유효하지 않습니다.")]},
                         market=market, venue="US" if market == "US" else "KRX",
                         safety=live_safety, definite_rejects=True)
    request = _nh_request(market, operation)
    call = broker.cancel_order if operation == "cancel" else broker.place_order
    with pytest.raises(InvalidResponseError if with_number else BrokerRejectedError):
        call(request)
    with pytest.raises(DuplicateOrderError):
        call(request)
    assert len(calls) == 1


@pytest.mark.parametrize("market", ["KR", "US"])
@pytest.mark.parametrize("operation", ["buy", "sell", "cancel"])
@pytest.mark.parametrize("code", UNKNOWN_CODES)
def test_ls_no_unproven_error_code_rejects(market, operation, code, unlocked, live_safety):  # noqa: F811
    tr = "COSAT00301" if market == "US" else ("CSPAT00801" if operation == "cancel" else "CSPAT00601")
    response = {"rsp_msg": "응답을 수신하지 못했습니다."}
    if code is not None:
        response["rsp_cd"] = code
    live_safety.valuation = lambda request: Decimal(1000)
    broker, client, _ = make_mock({tr: [response]}, market=market, venue="NASDAQ" if market == "US" else "KRX",
                                  order_date=date(2026, 10, 29), safety=live_safety, definite_rejects=True)
    kwargs = {"symbol": "AAPL", "market": "US", "venue": "NASDAQ", "quantity": 1} if market == "US" else {}
    request = cancel(**kwargs) if operation == "cancel" else buy(side=operation, **kwargs)
    call = broker.cancel_order if operation == "cancel" else broker.place_order
    with pytest.raises(InvalidResponseError):
        call(request)
    with pytest.raises(DuplicateOrderError):
        call(request)
    assert len(client.calls) == 1


@pytest.mark.parametrize("depth", [2, 4, 20])
@pytest.mark.parametrize("leaf", ["open", "filled", "cancelled"])
@pytest.mark.parametrize("reverse", [False, True])
def test_ls_full_correction_chain_never_closes_original(depth, leaf, reverse):
    rows = [_ls_row()]
    for offset in range(1, depth + 1):
        rows.append(_cancel_row(ordno=str(42 + offset), parent=str(41 + offset)))
    if leaf == "open":
        rows[-1].update(ordrem="2", cfmqty="0")
    elif leaf == "filled":
        rows[-1].update(cheqty="2")
    if reverse:
        rows.reverse()
    broker, _, _ = ls_make(_t0425(*rows), ledger_reads=True)
    observed = {o.id: o for o in broker.list_orders()}
    assert observed["42"].status == "unknown" and observed["42"].filled == 1
    if leaf == "open":
        assert observed[str(42 + depth)].remaining == 2


def test_ls_correction_chain_links_zero_padded_parent_numbers():
    rows = [_ls_row(ordno="00042"), _cancel_row(parent="00042"),
            _ls_row(ordno="44", qty=2, cheqty=0, ordrem=2, orgordno="00043")]
    broker, _, _ = ls_make(_t0425(*rows), ledger_reads=True)
    observed = {o.id: o for o in broker.list_orders()}
    assert observed["00042"].status == "unknown" and observed["44"].remaining == 2


def test_ls_correction_chain_can_cross_pages():
    first = _t0425(_ls_row(), _cancel_row())["t0425"][0]
    first["t0425OutBlock"]["cts_ordno"] = "43"
    first["_headers"] = {"tr_cont": "Y", "tr_cont_key": "next-page"}
    second = _t0425(_ls_row(ordno="44", qty=2, cheqty=0, ordrem=2, orgordno="43"))["t0425"][0]
    second["_headers"] = {"tr_cont": "N", "tr_cont_key": ""}
    broker, _, _ = ls_make({"t0425": [first, second]}, ledger_reads=True)
    assert {o.id: o.status for o in broker.list_orders()}["42"] == "unknown"


@pytest.mark.parametrize("extra", [
    _cancel_row(),  # repeated cancel row cannot count twice
    _cancel_row(ordno="42", parent="43"),  # cycle back to root
])
def test_ls_duplicate_or_cyclic_chain_is_not_terminal(extra):
    broker, _, _ = ls_make(_t0425(_ls_row(), _cancel_row(), extra), ledger_reads=True)
    with pytest.raises(InvalidResponseError):
        broker.list_orders()


@pytest.mark.parametrize("code", NH_REJECT_CODES)
@pytest.mark.parametrize("message", [None, "", "주문이 완료되었습니다.", "주문 접수", "주문 처리 중", "timeout"])
def test_nh_allowlisted_code_with_missing_or_conflicting_message_stays_unknown(code, message, live_safety):
    path = _nh_path("KR", "buy")
    broker, calls = make({path: [reply(code=code, message=message)]}, safety=live_safety, definite_rejects=True)
    with pytest.raises(InvalidResponseError):
        broker.place_order(order())
    assert len(calls) == 1


@pytest.mark.parametrize("market", ["KR", "US"])
@pytest.mark.parametrize("operation", ["buy", "sell", "cancel"])
def test_nh_success_with_rejection_switch_on_is_unchanged(market, operation, live_safety):
    path = _nh_path(market, operation)
    output = {"orr_no" if market == "US" else "mkt_orr_no": 123}
    live_safety.valuation = lambda request: Decimal(1000)
    broker, calls = make({path: [reply(output)]}, market=market, venue="US" if market == "US" else "KRX",
                         safety=live_safety, definite_rejects=True)
    call = broker.cancel_order if operation == "cancel" else broker.place_order
    assert call(_nh_request(market, operation)).status == "accepted"
    assert len(calls) == 1


@pytest.mark.parametrize("market", ["KR", "US"])
@pytest.mark.parametrize("operation", ["buy", "sell", "cancel"])
def test_ls_success_with_rejection_switch_on_is_unchanged(market, operation, unlocked, live_safety):  # noqa: F811
    tr = "COSAT00301" if market == "US" else ("CSPAT00801" if operation == "cancel" else "CSPAT00601")
    response = {"rsp_cd": "00000", tr + "OutBlock2": {"OrdNo": 43}}
    live_safety.valuation = lambda request: Decimal(1000)
    broker, client, _ = make_mock({tr: [response]}, market=market, venue="NASDAQ" if market == "US" else "KRX",
                                  order_date=date(2026, 10, 29), safety=live_safety, definite_rejects=True)
    kwargs = {"symbol": "AAPL", "market": "US", "venue": "NASDAQ", "quantity": 1} if market == "US" else {}
    request = cancel(**kwargs) if operation == "cancel" else buy(side=operation, **kwargs)
    call = broker.cancel_order if operation == "cancel" else broker.place_order
    assert call(request).status == "accepted"
    assert len(client.calls) == 1
