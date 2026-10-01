"""Second review regressions: fake transports only, under the socket blocker."""

import pytest

from pykbroker.brokers.nh import NH_API_BASE_URL, NH_MOCK_BASE_URL
from pykbroker.exceptions import BrokerRejectedError, InvalidResponseError, SafetyError
from pykbroker.models import CancelRequest, OrderRequest
from tests.test_nh_read_contract import BALANCE, make, reply
from tests.test_nh_sdk_finish import make as make_sdk

DAILY = "/krstock/inquiry/v1/dailyOrderExecution"
BALANCE_PATH = "/krstock/inquiry/v1/balance"


def request(operation):
    if operation.startswith("place"):
        return OrderRequest(symbol="005930", side="buy", quantity=1, price=70000, client_key="round2")
    return CancelRequest(symbol="005930", order_id="mkt:123", quantity=1, client_key="round2")


@pytest.mark.parametrize("virtual", [False, True])
def test_virtual_is_read_only(virtual):
    broker, calls, _ = make_sdk(virtual=virtual)
    with pytest.raises(AttributeError):
        broker.virtual = not virtual
    assert broker.virtual is virtual
    assert broker.capabilities().virtual is virtual
    assert calls == []


@pytest.mark.parametrize("factory", [False, True])
@pytest.mark.parametrize("operation", ["place_order", "place", "cancel_order", "cancel"])
def test_live_lock_uses_bound_host_despite_shadow_mode(factory, operation, live_safety):
    broker, calls, _ = make_sdk(factory=factory, safety=live_safety)
    # Shadow values that used to control the public flag cannot reclassify the host.
    broker.__dict__["virtual"] = True
    broker._virtual = True
    with pytest.raises(SafetyError, match="NH live orders"):
        getattr(broker, operation)(request(operation))
    assert broker.virtual is False
    assert calls == []


@pytest.mark.parametrize("virtual", [False, True])
@pytest.mark.parametrize("operation", ["place_order", "cancel_order"])
def test_order_mode_matches_verified_host_despite_shadow_mode(virtual, operation, live_safety):
    broker, calls, _ = make_sdk(virtual=virtual, safety=live_safety, allow_live_orders=True)
    broker.__dict__["virtual"] = not virtual
    broker._virtual = not virtual
    result = getattr(broker, operation)(request(operation))
    assert result.virtual is virtual
    assert broker.capabilities().virtual is virtual
    assert [(host, path) for host, path, _, _ in calls] == [
        (
            NH_MOCK_BASE_URL if virtual else NH_API_BASE_URL,
            "/krstock/order/v1/cashBuy" if operation == "place_order" else "/krstock/order/v1/cancel",
        )
    ]


@pytest.mark.parametrize("operation,path", [("list_orders", DAILY), ("get_positions", BALANCE_PATH)])
@pytest.mark.parametrize("message", ["내역 없음", "조회된 데이터가 존재하지 않습니다"])
def test_no_data_code_without_blocks_returns_empty(operation, path, message):
    broker, calls = make({path: reply({"rsp_cd": "13578", "rsp_msg": message})})
    assert getattr(broker, operation)() == []
    assert len(calls) == 1


@pytest.mark.parametrize("code", ["00000", "00166", "00221"])
def test_daily_missing_block_is_not_empty_for_other_success_codes(code):
    broker, _ = make({DAILY: reply({"rsp_cd": code, "rsp_msg": "조회 완료"})})
    with pytest.raises(InvalidResponseError):
        broker.list_orders()


@pytest.mark.parametrize("code", ["99999", None])
@pytest.mark.parametrize("message", ["기타 처리 완료", "조회 완료", "정상처리되었습니다"])
def test_unknown_read_code_cannot_be_promoted_by_completion(code, message):
    body = {"Output_0": BALANCE["Output_0"], "rsp_msg": message}
    if code is not None:
        body["rsp_cd"] = code
    broker, _ = make({BALANCE_PATH: reply(body)})
    with pytest.raises(BrokerRejectedError):
        broker.get_balance()


@pytest.mark.parametrize("operation", ["place_order", "cancel_order"])
@pytest.mark.parametrize("code", ["99999", None])
def test_unknown_order_code_cannot_be_promoted_by_completion(operation, code, live_safety):
    broker, calls, _ = make_sdk(virtual=True, safety=live_safety)
    body = {"Output_0": {"mkt_orr_no": 123}, "rsp_msg": "기타 처리 완료"}
    if code is not None:
        body["rsp_cd"] = code
    broker._client.post = lambda *args, **kwargs: calls.append(args) or (200, body, {})
    with pytest.raises(InvalidResponseError, match="unknown"):
        getattr(broker, operation)(request(operation))
    assert len(calls) == 1  # An ambiguous acknowledgement never causes replay.
