"""NH·LS order answers the official contract defines as «not accepted» become BrokerRejectedError (F2 D6738 ⑤).

NH: https://www.nhplug.com/openapi-docs/krstock/openapi.json ``x-response-status`` and https://www.nhplug.com/llms.txt
«성공 판정» (read 2026-10-28) — HTTP 200 may be a business error; success = expected Output block has a value and
rsp_msg means completion; rsp_cd is for logs (no code table). LS: official live client
https://openapi.ls-sec.co.kr/resource/playground/sample.html ``callRestTr`` (non ``/^0+$/`` rsp_cd = «요청 실패»),
TR guide 20261026021444748 (OrdNo in <TR>OutBlock2). Only answers with NO order number are definite; everything
else stays unknown (InvalidResponseError) so a possibly standing order is reconciled, never re-sent.
"""

import pytest

from pykbroker.exceptions import BrokerRejectedError, DuplicateOrderError, InvalidResponseError
from tests.test_ls_contract import buy, cancel, make_mock, page, unlocked  # noqa: F401  (fixture re-export)
from tests.test_nh_order_contract import ORDER, make, order, reply
from tests.test_nh_order_contract import cancel as nh_cancel

SECRET = "fixture-sensitive-detail"


# ----------------------------------------------------------------------------- NH


@pytest.mark.parametrize("endpoint,call", [("cashBuy", "place"), ("cancel", "cancel")])
def test_nh_failure_code_without_order_number_is_definite_rejection(endpoint, call, live_safety):
    body = {"rsp_cd": "90001", "message": {"usr_msg": "주문가능금액을 초과하였습니다. " + SECRET}}
    broker, calls = make({ORDER + endpoint: [(200, body, {})]}, safety=live_safety, definite_rejects=True)
    with pytest.raises(BrokerRejectedError) as raised:
        broker.place_order(order()) if call == "place" else broker.cancel_order(nh_cancel())
    assert raised.value.code == "90001" and raised.value.kind == "rejected"
    assert SECRET not in str(raised.value)
    assert [c[1] for c in calls] == [ORDER + endpoint]  # one POST, no replay


@pytest.mark.parametrize("output", [{}, {"mkt_orr_no": 0}, {"mkt_orr_no": None}])
def test_nh_failure_code_with_empty_block_is_definite_rejection(output, live_safety):
    broker, _ = make({ORDER + "cashBuy": [reply(output, code="90001", message="종목코드 항목을 입력하세요.")]},
                     safety=live_safety, definite_rejects=True)
    with pytest.raises(BrokerRejectedError):
        broker.place_order(order())


@pytest.mark.parametrize(
    "response",
    [
        reply({"mkt_orr_no": 123}, code="90001", message="주문가능금액을 초과하였습니다."),  # number came back
        reply(None, code="90001", message=""),                                          # no reason
        reply(None, code="90001", message="주문이 완료되지 않았습니다."),               # completion-like wording
        reply(None, code="90001", message="정상처리되었습니다"),                         # completion wording
        reply(None, code=None, message="주문가능금액을 초과하였습니다."),               # no code
        reply(None, code="00000", message="주문가능금액을 초과하였습니다."),            # success code
        reply(None, code="00165", message="조회가 계속됩니다"),                          # continuation code
        reply(None, code="90009", message="주문 처리 지연 중입니다. 주문내역을 확인하세요."),  # delay wording
        reply(None, code="90010", message="시간 초과"),                                  # timeout wording
    ],
)
def test_nh_other_answers_stay_unknown(response, live_safety):
    broker, calls = make({ORDER + "cashBuy": [response]}, safety=live_safety, definite_rejects=True)
    with pytest.raises(InvalidResponseError):
        broker.place_order(order())
    assert len(calls) == 1


# ----------------------------------------------------------------------------- LS


@pytest.mark.parametrize("operation,tr", [("place_order", "CSPAT00601"), ("cancel_order", "CSPAT00801")])
@pytest.mark.parametrize("block", [None, {}, {"OrdNo": 0}, {"OrdNo": "0000"}, {"OrdNo": None}])
def test_ls_nonzero_code_with_reason_and_no_order_number_is_definite_rejection(
    unlocked, live_safety, operation, tr, block  # noqa: F811
):
    response = {"rsp_cd": "03001", "rsp_msg": "주문가능금액을 초과하였습니다. " + SECRET}
    if block is not None:
        response[tr + "OutBlock2"] = block
    broker, client, _ = make_mock({tr: [response, page(tr)]}, safety=live_safety, definite_rejects=True)
    request = buy() if operation.startswith("place") else cancel()
    with pytest.raises(BrokerRejectedError) as raised:
        getattr(broker, operation)(request)
    assert raised.value.code == "03001" and raised.value.kind == "rejected"
    assert SECRET not in str(raised.value)
    with pytest.raises(DuplicateOrderError):  # the same client key is never re-sent
        getattr(broker, operation)(request)
    assert len(client.calls) == 1


@pytest.mark.parametrize("operation,tr", [("place_order", "CSPAT00601"), ("cancel_order", "CSPAT00801")])
@pytest.mark.parametrize("response", [
    {"rsp_cd": "00039", "rsp_msg": "매수주문이 완료되었습니다.", "BLOCK": {"OrdNo": 42}},  # number came back
    {"rsp_cd": "00039", "rsp_msg": "매수주문이 완료되었습니다."},     # «완료» without the expected block (P1-1)
    {"rsp_cd": "00040", "rsp_msg": "매도주문이 접수되었습니다."},     # «접수» wording
    {"rsp_cd": "09999", "rsp_msg": "주문 처리 지연 중입니다. 주문내역을 확인하세요."},  # delay wording
    {"rsp_cd": "09998", "rsp_msg": "Timeout"},                          # timeout wording
])
def test_ls_uncertain_answers_stay_unknown(unlocked, live_safety, operation, tr, response):  # noqa: F811
    response = {(tr + "OutBlock2" if k == "BLOCK" else k): v for k, v in response.items()}
    broker, client, _ = make_mock({tr: [response]}, safety=live_safety, definite_rejects=True)
    with pytest.raises(InvalidResponseError):
        getattr(broker, operation)(buy() if operation.startswith("place") else cancel())
    assert len(client.calls) == 1


# ----------------------------------------------------------------------------- NH cancel confirmation (D6738 ③)

from tests.test_nh_order_contract import DAILY, US_ORDERS, us_row  # noqa: E402


def _kr_row(**changes):
    return {"itg_orr_no": 456, "iem_cd": "005930", "sby_dit_cd_nm": "매수", "orr_qty": 3, "orr_pr": 70000,
            "rmt_mkt_cd": "KRX", "org_itg_orr_no": "0", "tot_cns_qty": 1, "ny_cns_qty": 0, "can_qty": 2, **changes}


def _kr_list(*rows):
    page = reply(list(rows), message="조회완료", code="00000")
    broker, _ = make({DAILY: [page]})
    return broker.list_orders()


@pytest.mark.parametrize("filled,cancelled", [(0, 3), (1, 2), (2, 1)])
def test_nh_kr_original_row_with_closed_quantities_is_cancelled(filled, cancelled):
    (listed,) = _kr_list(_kr_row(tot_cns_qty=filled, can_qty=cancelled))
    assert (listed.status, listed.filled, listed.remaining) == ("cancelled", filled, 0)


def test_nh_kr_cancel_child_row_does_not_veto_the_closed_original():
    child = {"itg_orr_no": 789, "iem_cd": "005930", "sby_dit_cd_nm": "매수", "orr_qty": 2, "orr_pr": 70000,
             "rmt_mkt_cd": "KRX", "org_itg_orr_no": 456, "tot_cns_qty": 0, "ny_cns_qty": 0}
    statuses = {o.id: o.status for o in _kr_list(_kr_row(), child)}
    assert statuses == {"itg:456": "cancelled", "itg:789": "unknown"}


@pytest.mark.parametrize("changes", [
    {"ny_cns_qty": 1},                       # still open
    {"can_qty": 1},                          # quantities do not close
    {"cor_qty": 1},                          # correction
    {"orr_rjt_rsn_cd_nm": "잔고부족"},       # rejection
    {"cor_can_dit_cd_nm": "정정"},           # legacy change marker
    {"org_itg_orr_no": 123},                 # a child row is never the confirmed original
])
def test_nh_kr_other_cancel_shapes_stay_unknown(changes):
    listed = [o for o in _kr_list(_kr_row(**changes)) if o.id == "itg:456"]
    assert listed[0].status == "unknown"


def test_nh_kr_status_by_terms_confirms_cancel_of_the_acknowledged_order():
    from pykbroker.models import Order

    page = reply([_kr_row()], message="조회완료", code="00000")
    broker, _ = make({DAILY: [page]})
    mine = Order(id="mkt:5001", symbol="005930", side="buy", quantity=3, price="70000", status="accepted")
    seen = broker.status(mine)
    assert (seen.id, seen.status, seen.filled) == ("mkt:5001", "cancelled", 1)


@pytest.mark.parametrize("row,state", [
    (us_row(cns_qty=1, ny_cns_orr_qty=0, can_qty=1), "cancelled"),
    (us_row(cns_qty=0, ny_cns_orr_qty=0, can_qty=2), "cancelled"),
    (us_row(cns_qty=0, ny_cns_orr_qty=0, can_qty=2, cor_can_dit_cd="2", orr_sts_nm="fixture"), "cancelled"),
    (us_row(cns_qty=1, ny_cns_orr_qty=0, can_qty=1, cor_qty=1), "unknown"),
    (us_row(cns_qty=1, ny_cns_orr_qty=0, can_qty=1, rjt_rsn_cts="fixture"), "unknown"),
    (us_row(cns_qty=1, ny_cns_orr_qty=0, can_qty=1, oss_ato_orr_sts_cd="2"), "unknown"),
    (us_row(cns_qty=1, ny_cns_orr_qty=1, can_qty=1), "unknown"),
    (us_row(cns_qty=1, ny_cns_orr_qty=0, can_qty=1, org_orr_no=99), "unknown"),
])
def test_nh_us_cancel_confirmation(row, state):
    broker, _ = make({US_ORDERS: [reply([row])]}, market="US", venue="US")
    listed = [o for o in broker.list_orders() if o.id == "us:123"]
    assert listed[0].status == state


# ---------------------------------------------------------------- LS ledger reads + cancel confirmation (D6738 ③④)

from tests.test_ls_contract import make as ls_make  # noqa: E402


def _t0425(*rows):
    return {"t0425": [{"rsp_cd": "00000", "rsp_msg": "조회완료", "t0425OutBlock": {"cts_ordno": ""},
                       "t0425OutBlock1": list(rows)}]}


def _ls_row(ordno="42", qty=3, cheqty=1, ordrem=0, **extra):
    return {"ordno": ordno, "expcode": "005930", "qty": str(qty), "price": "70000", "cheqty": str(cheqty),
            "cheprice": "70000", "ordrem": str(ordrem), "cfmqty": "0", "orgordno": "0", **extra}


def _cancel_row(ordno="43", parent="42", qty=2, cfmqty=2):
    return _ls_row(ordno=ordno, qty=qty, cheqty=0, ordrem=0, cfmqty=str(cfmqty), orgordno=parent)


def test_ls_default_instance_keeps_quantities_locked():
    broker, _, _ = ls_make(_t0425(_ls_row(cheqty=3)))
    (order,) = broker.list_orders()
    assert (order.quantity, order.filled, order.status) == (None, None, "unknown")


@pytest.mark.parametrize("row,state,filled", [
    (_ls_row(cheqty=3, ordrem=0), "filled", 3),
    (_ls_row(cheqty=1, ordrem=2), "partial", 1),
    (_ls_row(cheqty=0, ordrem=3), "open", 0),
    (_ls_row(cheqty=1, ordrem=0), "unknown", 1),   # zero remaining without a cancel row is never a fill
])
def test_ls_ledger_reads_opt_in_reads_official_quantities(row, state, filled):
    broker, _, _ = ls_make(_t0425(row), ledger_reads=True)
    (order,) = broker.list_orders()
    assert (order.status, order.filled) == (state, filled)


def test_ls_confirmed_cancel_child_closes_the_original_as_cancelled():
    broker, _, _ = ls_make(_t0425(_ls_row(), _cancel_row()), ledger_reads=True)
    statuses = {o.id: (o.status, o.filled) for o in broker.list_orders()}
    assert statuses["42"] == ("cancelled", 1)
    assert statuses["43"][0] == "unknown"


@pytest.mark.parametrize("child", [
    _cancel_row(cfmqty=0),                                          # cancel not confirmed yet
    _cancel_row(qty=1, cfmqty=1),                                    # only part cancelled: quantities do not close
    _ls_row(ordno="43", qty=2, cheqty=0, ordrem=2, orgordno="42"),   # correction: a live child order
    _ls_row(ordno="43", qty=2, cheqty=2, ordrem=0, cfmqty="2", orgordno="42"),  # correction child that executed
])
def test_ls_other_child_shapes_keep_the_original_unknown(child):
    broker, _, _ = ls_make(_t0425(_ls_row(), child), ledger_reads=True)
    assert {o.id: o.status for o in broker.list_orders()}["42"] == "unknown"


def test_ls_ledger_reads_does_not_unlock_an_unverified_virtual_instance():
    from pykbroker.brokers import ls as module

    broker, client, _ = ls_make(_t0425(_ls_row(cheqty=3)))
    client.base_url = module.LS_MOCK_BASE_URL
    broker._bound_virtual = True  # an unverified mock: the opt-in is for real accounts only
    broker._ledger_reads = True
    assert broker._reads_verified() is False


# ----------------------------------------------------------------------------- switch OFF by default (D6301)


@pytest.mark.parametrize("endpoint,call", [("cashBuy", "place"), ("cancel", "cancel")])
def test_nh_definite_rejects_is_off_by_default(endpoint, call, live_safety):
    body = {"rsp_cd": "90001", "message": {"usr_msg": "주문가능금액을 초과하였습니다."}}
    broker, calls = make({ORDER + endpoint: [(200, body, {})]}, safety=live_safety)
    with pytest.raises(InvalidResponseError) as raised:
        broker.place_order(order()) if call == "place" else broker.cancel_order(nh_cancel())
    assert not isinstance(raised.value, BrokerRejectedError)
    assert len(calls) == 1


@pytest.mark.parametrize("operation,tr", [("place_order", "CSPAT00601"), ("cancel_order", "CSPAT00801")])
def test_ls_definite_rejects_is_off_by_default(unlocked, live_safety, operation, tr):  # noqa: F811
    response = {"rsp_cd": "03001", "rsp_msg": "주문가능금액을 초과하였습니다."}
    broker, client, _ = make_mock({tr: [response]}, safety=live_safety)
    with pytest.raises(InvalidResponseError) as raised:
        getattr(broker, operation)(buy() if operation.startswith("place") else cancel())
    assert not isinstance(raised.value, BrokerRejectedError)
    assert len(client.calls) == 1


@pytest.mark.parametrize("value", [1, "yes", None])
def test_definite_rejects_must_be_a_bool(value):
    from pykbroker.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError):
        make({}, definite_rejects=value)
    with pytest.raises(ConfigurationError):
        ls_make({}, definite_rejects=value)
