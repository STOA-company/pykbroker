"""KB processFlag follows each TR's official success example (trader #651 cross review P1-3).

Official B2C JSON examples: SSAM1801 (KR sell), SSQM2341 (KR order list), SKAM2102 (US cancel) carry
processFlag "B" with resultCode "200"/"성공"; SSAM1802 (KR buy) and the rest carry "A". The guide never
defines the flag (ASSUMED), so a TR accepts only its own example's flag — the other one stays unknown.
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


def test_kr_sell_succeeds_with_documented_flag_b(live_safety):
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = _accepted("B")
    order = _broker(client, live_safety).place_order(
        OrderRequest(symbol="005930", side="sell", quantity=1, price="70000", client_key="kb-sell-b"))
    assert order.id == "123" and order.status == "accepted"
    assert client.post.call_args.args == ("/api/v1/ssam1801",)


def test_kr_sell_flag_a_is_not_the_documented_success(live_safety):
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = _accepted("A")
    with pytest.raises(InvalidResponseError):
        _broker(client, live_safety).place_order(
            OrderRequest(symbol="005930", side="sell", quantity=1, price="70000", client_key="kb-sell-a"))
    assert client.post.call_count == 1


def test_kr_buy_keeps_documented_flag_a(request_order, live_safety):
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = _accepted("A")
    assert _broker(client, live_safety).place_order(request_order).id == "123"


def test_kr_order_list_reads_with_documented_flag_b(live_safety):
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = {"dataHeader": {"resultCode": "200", "processFlag": "B"},
                                "dataBody": {"Record1": [], "grid_cnt1": "0", "nxt_key": ""}}
    assert _broker(client, live_safety).list_orders() == []
    assert client.post.call_args.args == ("/api/v1/ssqm2341",)
    client.post.return_value = {"dataHeader": {"resultCode": "200", "processFlag": "A"},
                                "dataBody": {"Record1": [], "grid_cnt1": "0", "nxt_key": ""}}
    with pytest.raises(InvalidResponseError):
        _broker(client, live_safety).list_orders()
