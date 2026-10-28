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
    broker, calls = make({ORDER + endpoint: [(200, body, {})]}, safety=live_safety)
    with pytest.raises(BrokerRejectedError) as raised:
        broker.place_order(order()) if call == "place" else broker.cancel_order(nh_cancel())
    assert raised.value.code == "90001" and raised.value.kind == "rejected"
    assert SECRET not in str(raised.value)
    assert [c[1] for c in calls] == [ORDER + endpoint]  # one POST, no replay


@pytest.mark.parametrize("output", [{}, {"mkt_orr_no": 0}, {"mkt_orr_no": None}])
def test_nh_failure_code_with_empty_block_is_definite_rejection(output, live_safety):
    broker, _ = make({ORDER + "cashBuy": [reply(output, code="90001", message="종목코드 항목을 입력하세요.")]},
                     safety=live_safety)
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
    ],
)
def test_nh_other_answers_stay_unknown(response, live_safety):
    broker, calls = make({ORDER + "cashBuy": [response]}, safety=live_safety)
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
    broker, client, _ = make_mock({tr: [response, page(tr)]}, safety=live_safety)
    request = buy() if operation.startswith("place") else cancel()
    with pytest.raises(BrokerRejectedError) as raised:
        getattr(broker, operation)(request)
    assert raised.value.code == "03001" and raised.value.kind == "rejected"
    assert SECRET not in str(raised.value)
    with pytest.raises(DuplicateOrderError):  # the same client key is never re-sent
        getattr(broker, operation)(request)
    assert len(client.calls) == 1


@pytest.mark.parametrize("operation,tr", [("place_order", "CSPAT00601"), ("cancel_order", "CSPAT00801")])
def test_ls_nonzero_code_with_order_number_stays_unknown(unlocked, live_safety, operation, tr):  # noqa: F811
    response = {"rsp_cd": "00039", "rsp_msg": "매수주문이 완료되었습니다.", tr + "OutBlock2": {"OrdNo": 42}}
    broker, client, _ = make_mock({tr: [response]}, safety=live_safety)
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
