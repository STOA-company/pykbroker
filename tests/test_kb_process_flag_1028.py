"""KB processFlag: "A" is the completed business result, "B" is a business error (D6393).

In the official B2C JSON examples every "B" response carries an error o_msg (e.g. SSAM1801 "제권리 수량이 없습니다.",
SKAM2102 "미접수주문은 정정/취소주문이 불가능합니다."); normal completions are "A". Anything else is unknown - reconcile.
"""
from unittest.mock import Mock

import pytest

from pykbroker.brokers.kb import KB, KB_API_BASE_URL
from pykbroker.exceptions import InvalidResponseError
from pykbroker.models import OrderRequest


def _broker(client, safety, market="KR"):
    return KB(account="fixture", bound_account="fixture", app_key="fake-app", token="fake-token",
              client=client, safety=safety, writes_enabled=True, market=market)


def _accepted(flag):
    return {"dataHeader": {"resultCode": "200", "processFlag": flag, "resultMessage": "성공"},
            "dataBody": {"ordr_no": "123", "is_cd": "005930", "ordr_q": "1"}}


def test_kr_sell_flag_a_succeeds(live_safety):
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = _accepted("A")
    order = _broker(client, live_safety).place_order(
        OrderRequest(symbol="005930", side="sell", quantity=1, price="70000", client_key="kb-sell-a"))
    assert order.id == "123" and order.status == "accepted"
    assert client.post.call_args.args == ("/api/v1/ssam1801",)


@pytest.mark.parametrize("flag", ["B", "C", "", None])
def test_kr_sell_non_a_flag_is_unknown(live_safety, flag):
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = _accepted(flag)
    with pytest.raises(InvalidResponseError):
        _broker(client, live_safety).place_order(
            OrderRequest(symbol="005930", side="sell", quantity=1, price="70000", client_key="kb-sell-b"))
    assert client.post.call_count == 1


def test_kr_buy_flag_a(request_order, live_safety):
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = _accepted("A")
    assert _broker(client, live_safety).place_order(request_order).id == "123"


def test_kr_order_list_a_reads_b_fails(live_safety):
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = {"dataHeader": {"resultCode": "200", "processFlag": "A"},
                                "dataBody": {"Record1": [], "grid_cnt1": "0", "nxt_key": ""}}
    assert _broker(client, live_safety).list_orders() == []
    assert client.post.call_args.args == ("/api/v1/ssqm2341",)
    client.post.return_value = {"dataHeader": {"resultCode": "200", "processFlag": "B"},
                                "dataBody": {"Record1": [], "grid_cnt1": "0", "nxt_key": ""}}
    with pytest.raises(InvalidResponseError):
        _broker(client, live_safety).list_orders()
