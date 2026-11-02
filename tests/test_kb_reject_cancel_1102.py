"""KB: SSAM1801 공식 거절 샘플만 확정 거절, SSQM2341 정정취소 구분값 미확정으로 잔량 취소는 모름 유지 (F2 D10839)."""
from unittest.mock import Mock

import pytest

from pykbroker.brokers.kb import KB, KB_API_BASE_URL
from pykbroker.exceptions import BrokerRejectedError, InvalidResponseError
from pykbroker.models import OrderRequest


def _broker(client, safety):
    return KB(account="fixture", bound_account="fixture", app_key="fake-app", token="fake-token",
              client=client, safety=safety, writes_enabled=True, market="KR")


def _refusal(code="1853", number="0000000000", flag="B", result="200"):
    return {"dataHeader": {"resultCode": result, "processFlag": flag, "processCode": code},
            "dataBody": {"ordr_no": number, "o_msg": "제권리 수량이 없습니다."}}


def _sell(live_safety, answer, key="kb-1102"):
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = answer
    broker = _broker(client, live_safety)
    request = OrderRequest(symbol="005930", side="sell", quantity=1, price="70000", client_key=key)
    return broker, client, request


def test_official_1853_on_sell_is_definite_reject_single_post(live_safety):
    broker, client, request = _sell(live_safety, _refusal())
    with pytest.raises(BrokerRejectedError) as info:
        broker.place_order(request)
    assert info.value.code == "1853" and client.post.call_count == 1


@pytest.mark.parametrize("answer", [
    _refusal(code="9999"), _refusal(number="0000000123"), _refusal(flag="A"), _refusal(flag="C"),
    _refusal(result="400"), _refusal(number=""),
])
def test_everything_else_stays_unknown(live_safety, answer):
    broker, client, request = _sell(live_safety, answer)
    with pytest.raises((InvalidResponseError, Exception)) as info:
        broker.place_order(request)
    assert not isinstance(info.value, BrokerRejectedError) and client.post.call_count == 1


def test_1853_on_buy_tr_is_not_a_definite_reject(live_safety):
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = _refusal()
    request = OrderRequest(symbol="005930", side="buy", quantity=1, price="70000", client_key="kb-buy-1102")
    with pytest.raises(Exception) as info:
        _broker(client, live_safety).place_order(request)
    assert not isinstance(info.value, BrokerRejectedError)


def _row(no, q, filled, rest, **extra):
    return {"stnd_is_no": "KR7005930003", "ordr_no": no, "ordr_q": str(q), "tl_ccls_q": str(filled),
            "nccls_q": str(rest), "ordr_uprc": "70000", "ccls_uprc": "70000", **extra}


def _list(live_safety, rows):
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.side_effect = [
        {"dataHeader": {"resultCode": "200", "processFlag": "A"},
         "dataBody": {"Record1": rows, "grid_cnt1": str(len(rows)), "nxt_key": ""}},
        {"dataHeader": {"resultCode": "200", "processFlag": "A"},
         "dataBody": {"stnd_is_cd": "KR7005930003", "shrt_is_cd": "005930"}},
    ] * 3
    return {o.id: o.status for o in _broker(client, live_safety).list_orders()}


CANCEL = dict(orgn_ordr_no="1001", crct_cncl_ccd="2", rfsl_rsn_nm="", crct_q="0")


@pytest.mark.xfail(strict=True, reason="KB 정정취소구분 값 미확정, 회사 시험계좌 실측 필요")
def test_partial_with_linked_cancel_row_is_cancelled(live_safety):
    got = _list(live_safety, [_row("1001", 3, 1, 0), _row("1002", 2, 0, 0, **CANCEL)])
    assert got["1001"] == "cancelled"


@pytest.mark.parametrize("bad_cancel", [
    {**CANCEL, "crct_cncl_ccd": "1"}, {**CANCEL, "crct_cncl_ccd": "2"},
    {**CANCEL, "rfsl_rsn_nm": "거부"}, {**CANCEL, "crct_cncl_ccd": ""}, {**CANCEL, "orgn_ordr_no": "9999"},
])
def test_partial_without_valid_cancel_evidence_stays_unknown(live_safety, bad_cancel):
    got = _list(live_safety, [_row("1001", 3, 1, 0), _row("1002", 2, 0, 0, **bad_cancel)])
    assert got["1001"] == "unknown"


def test_cancel_row_with_wrong_quantity_or_open_stays_unknown(live_safety):
    assert _list(live_safety, [_row("1001", 3, 1, 0), _row("1002", 1, 0, 0, **CANCEL)])["1001"] == "unknown"
    assert _list(live_safety, [_row("1001", 3, 1, 0), _row("1002", 2, 0, 2, **CANCEL)])["1001"] == "unknown"


def test_no_linked_row_zero_filled_and_still_open_unchanged(live_safety):
    assert _list(live_safety, [_row("1001", 3, 1, 0)])["1001"] == "unknown"
    assert _list(live_safety, [_row("1001", 3, 0, 0, **{}), _row("1002", 3, 0, 0, **CANCEL)])["1001"] == "unknown"
    assert _list(live_safety, [_row("1001", 3, 1, 2)])["1001"] == "partial"
