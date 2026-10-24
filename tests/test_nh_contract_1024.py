"""Offline official-shape contract; synthetic codes/data are not live evidence.

Reuses the existing scripted NH transport. No network, keys, or account access.
"""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from pykbroker.exceptions import (
    BrokerRejectedError,
    ConfigurationError,
    DuplicateOrderError,
    InvalidResponseError,
    NotSupportedError,
    TransportError,
)
from pykbroker.models import CancelRequest, OrderRequest
from tests.test_nh_official_contract import check_fields
from tests.test_nh_order_contract import DAILY, ORDER, make
from tests.test_nh_read_contract import make as make_read

SCHEMA = json.loads((Path(__file__).parent / "fixtures/nh/official/krstock-1024.json").read_text())


def resolved(value):
    if isinstance(value, dict):
        if "$ref" in value:
            return resolved(SCHEMA["components"]["schemas"][value["$ref"].split("/")[-1]])
        return {key: resolved(item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolved(item) for item in value]
    return value


def response(path, output, code="00000"):
    body = {"message": {"msg_code": code, "usr_msg": "정상처리되었습니다"}, "Output_0": output}
    schema = SCHEMA["paths"][path]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    check_fields(resolved(schema), body)
    return 200, body, {}


def check_requests(calls):
    for _, path, _, body in calls:
        schema = SCHEMA["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
        check_fields(resolved(schema), body)


def request(side="buy", kind="limit", key="place", quantity=1):
    return OrderRequest(symbol="005930", side=side, quantity=quantity,
                        order_type=kind, price=Decimal(1000) if kind == "limit" else None,
                        client_key=key)


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("kind", ["limit", "market"])
def test_official_cash_order_shape_and_duplicate_key(side, kind, live_safety):
    path = ORDER + ("cashBuy" if side == "buy" else "cashSell")
    broker, calls = make({path: [response(path, {"mkt_orr_no": 101})]}, virtual=True, safety=live_safety)
    live_safety.market_order_allowed = lambda _: True
    live_safety.valuation = lambda _: Decimal(1000)
    req = request(side, kind)
    placed = broker.place_order(req)
    assert (placed.id, placed.order_type, placed.side) == ("mkt:101", kind, side)
    with pytest.raises(DuplicateOrderError):
        broker.place_order(req)
    assert len(calls) == 1
    wire = calls[0][3]["Input_0"]
    assert wire["nmn_pr_tp_cd"] == ("05" if kind == "market" else "01")
    assert ("orr_pr" in wire) is (kind == "limit")
    assert ("orr_amt" in wire) is (kind == "limit")
    check_requests(calls)
    # No invented equality of market and integrated order numbers.
    assert broker.status(placed).status == "unknown"
    assert len(calls) == 1


@pytest.mark.parametrize("venue", ["KRX", "NXT"])
def test_partial_then_modify_and_cancel_requested_remainder(venue, live_safety):
    row = {"itg_orr_no": 999, "iem_cd": "005930", "orr_qty": 3, "orr_pr": 1000,
           "tot_cns_qty": 1, "ny_cns_qty": 2, "cns_avg_uit_pr": 990,
           "sby_dit_cd_nm": "매수", "nmn_pr_tp_cd_nm": "보통가", "rmt_mkt_cd": venue}
    broker, calls = make({
        DAILY: [response(DAILY, [row])],
        ORDER + "modify": [response(ORDER + "modify", {"mkt_orr_no": 102})],
        ORDER + "cancel": [response(ORDER + "cancel", {"mkt_orr_no": 103})],
    }, virtual=True, safety=live_safety, venue=venue)
    observed, = broker.list_orders()
    assert (observed.status, observed.filled, observed.remaining, observed.order_type) == ("partial", 1, 2, "limit")
    # The original market number is caller-supplied, never inferred from itg:999.
    req = request(key="modify", quantity=2)
    modified = broker.modify_order("mkt:101", req, stop_condition_price=Decimal(0))
    assert modified.id == "mkt:102" and modified.status == "accepted"
    with pytest.raises(DuplicateOrderError):
        broker.modify_order("mkt:101", req, stop_condition_price=Decimal(0))
    cancelled = broker.cancel_order(CancelRequest(
        order_id=modified.id, symbol="005930", quantity=2, client_key="cancel"
    ))
    assert cancelled.status == "accepted"
    assert calls[1][3] == {"Input_0": {
        "act_no": "12345678901", "org_mkt_orr_no": 101, "all_pat_dit_cd": "2",
        "iem_cd": "005930", "cor_qty": 2, "cor_pr": 1000, "sop_cnd_pr": 0.0,
        "rmt_mkt_cd": venue, "sor_mkt_sli_yn": "N",
    }}
    assert calls[2][3]["Input_0"]["org_mkt_orr_no"] == 102
    assert calls[1][3]["Input_0"]["cor_qty"] == calls[2][3]["Input_0"]["cor_qty"] == 2
    check_requests(calls)


@pytest.mark.parametrize("code", ["IGW40019", "IGW40031", "IGW40032", "IGW42901", "IGW42902", "IGW42903"])
def test_synthetic_official_error_code_envelope_never_acknowledges_or_replays_order(code, live_safety):
    path = ORDER + "cashBuy"
    broker, calls = make({path: [response(path, {"mkt_orr_no": 101}, code)]}, safety=live_safety)
    with pytest.raises(InvalidResponseError):
        broker.place_order(request())
    with pytest.raises(DuplicateOrderError):
        broker.place_order(request())
    assert len(calls) == 1


@pytest.mark.parametrize("code", ["IGW40043", "IGW40044"])
def test_token_rejection_discards_token_without_order_replay(code, live_safety):
    path = ORDER + "cashBuy"
    broker, calls = make({path: [response(path, {}, code)]}, safety=live_safety)
    with pytest.raises(BrokerRejectedError):
        broker.place_order(request())
    assert broker.token_state()["token"] == "" and len(calls) == 1


def test_expired_official_envelope_refreshes_read_once():
    broker, calls = make_read({
        DAILY: [response(DAILY, [], "IGW40044"), response(DAILY, [])],
        "/oauth2/token": [(200, {"access_token": "synthetic-refreshed", "expires_in": 86400}, {})],
    }, options={"app_key": "synthetic-key", "app_secret": "synthetic-secret", "token": "synthetic-old"})
    assert broker.list_orders() == []
    assert [item[1] for item in calls] == [DAILY, "/oauth2/token", DAILY]


def test_conflicting_official_and_legacy_codes_fail_closed(live_safety):
    path = ORDER + "cashBuy"
    status, body, headers = response(path, {"mkt_orr_no": 101})
    body["rsp_cd"] = "IGW40019"
    broker, calls = make({path: [(status, body, headers)]}, safety=live_safety)
    with pytest.raises(InvalidResponseError, match="conflict"):
        broker.place_order(request())
    assert len(calls) == 1


@pytest.mark.parametrize("status", [429, 500])
def test_order_http_failure_is_not_retried(status, live_safety):
    path = ORDER + "cashBuy"
    broker, calls = make({path: [(status, {}, {})]}, safety=live_safety)
    with pytest.raises(TransportError):
        broker.place_order(request())
    assert len(calls) == 1


@pytest.mark.parametrize("order_id", ["itg:999", "mkt:0", "mkt:bad"])
def test_modify_requires_valid_original_market_number(order_id, live_safety):
    broker, calls = make({}, safety=live_safety)
    with pytest.raises((ConfigurationError, InvalidResponseError)):
        broker.modify_order(order_id, request(), stop_condition_price=Decimal(0))
    assert calls == []


def test_modify_revalidates_model_and_disallows_market(live_safety):
    broker, calls = make({}, safety=live_safety)
    with pytest.raises(NotSupportedError):
        broker.modify_order("mkt:101", request(kind="market"), stop_condition_price=Decimal(0))
    with pytest.raises(ValueError):
        broker.modify_order("mkt:101", request().model_copy(update={"quantity": 0}), stop_condition_price=Decimal(0))
    assert calls == []


@pytest.mark.parametrize("message", [
    "주문 실패", "주문 오류", "처리 에러", "주문 미완료", "주문가능금액이 부족합니다.",
    "종목코드 항목을 입력하세요.", "", None, "오류 처리 완료",
])
def test_success_code_and_number_never_override_failure_message(message, live_safety):
    path = ORDER + "cashBuy"
    status, body, headers = response(path, {"mkt_orr_no": 101})
    body["message"]["usr_msg"] = message
    broker, calls = make({path: [(status, body, headers)]}, safety=live_safety)
    with pytest.raises(InvalidResponseError):
        broker.place_order(request())
    assert len(calls) == 1


@pytest.mark.parametrize("message", ["완료", "주문 완료되었습니다.", "정상처리되었습니다", "주문 접수되었습니다."])
def test_write_requires_explicit_completion_message(message, live_safety):
    path = ORDER + "cashBuy"
    status, body, headers = response(path, {"mkt_orr_no": 101})
    body["message"]["usr_msg"] = message
    broker, calls = make({path: [(status, body, headers)]}, safety=live_safety)
    assert broker.place_order(request()).status == "accepted"
    assert len(calls) == 1


@pytest.mark.parametrize("operation", ["read", "write"])
def test_legacy_success_message_cannot_hide_official_failure(operation, live_safety):
    path = DAILY if operation == "read" else ORDER + "cashBuy"
    status, body, headers = response(path, [] if operation == "read" else {"mkt_orr_no": 101})
    body.update(rsp_msg="정상처리되었습니다")
    body["message"]["usr_msg"] = "주문 실패"
    broker, calls = make({path: [(status, body, headers)]}, safety=live_safety)
    with pytest.raises(InvalidResponseError, match="messages conflict"):
        broker.list_orders() if operation == "read" else broker.place_order(request())
    assert len(calls) == 1


def test_stateful_cash_order_partial_modify_cancel_balance_and_positions(live_safety):
    """Synthetic mkt/itg association belongs to this server only, not the adapter."""
    balance_path = "/krstock/inquiry/v1/balance"
    sellable_path = "/krstock/inquiry/v1/sellableQuantity"
    state = {"cash": 10000, "held": 0, "filled": 0, "remaining": 0,
             "cancelled": 0, "market_number": 100, "quantity": 0, "price": 0}
    calls = []

    def wire():
        return json.loads(calls[-1][3])["Input_0"]

    def place():
        data = wire()
        state.update(quantity=data["orr_qty"], remaining=data["orr_qty"], price=data["orr_pr"])
        state["market_number"] += 1
        return response(ORDER + "cashBuy", {"mkt_orr_no": state["market_number"]})

    def modify():
        data = wire()
        assert data["org_mkt_orr_no"] == state["market_number"]
        assert data["cor_qty"] == state["remaining"]
        state["price"] = data["cor_pr"]
        state["market_number"] += 1
        return response(ORDER + "modify", {"mkt_orr_no": state["market_number"]})

    def cancel_remaining():
        data = wire()
        assert data["org_mkt_orr_no"] == state["market_number"]
        assert data["cor_qty"] <= state["remaining"]
        state["cancelled"] += data["cor_qty"]
        state["remaining"] -= data["cor_qty"]
        return response(ORDER + "cancel", {"mkt_orr_no": state["market_number"] + 1})

    def ledger():
        return response(DAILY, [{
            "itg_orr_no": 9001, "iem_cd": "005930", "orr_qty": state["quantity"],
            "orr_pr": state["price"], "tot_cns_qty": state["filled"],
            "ny_cns_qty": state["remaining"], "can_qty": state["cancelled"],
            "cns_avg_uit_pr": 990 if state["filled"] else 0,
        }])

    def balance():
        status, body, headers = response(balance_path, {
            "dca": state["cash"], "orr_pbl_amt4": state["cash"], "tot_eal_amt": state["held"] * 990,
        })
        body["Output_1"] = [{"iem_cd": "005930", "itg_bnc_qty": state["held"],
                             "phs_pr": 990, "now_pr": 990, "eal_amt": state["held"] * 990}]
        schema = SCHEMA["paths"][balance_path]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
        check_fields(resolved(schema), body)
        return status, body, headers

    broker, calls = make_read({
        ORDER + "cashBuy": place, ORDER + "modify": modify, ORDER + "cancel": cancel_remaining,
        DAILY: ledger, balance_path: balance,
        sellable_path: lambda: response(sellable_path, {"iem_cd": "005930", "sll_pbl_qty": state["held"]}),
    }, virtual=True)
    broker.safety = live_safety
    placed = broker.place_order(request(quantity=3))
    assert state["remaining"] == 3 and placed.id == "mkt:101"
    # Explicit simulator fill event: not a claim about NH's actual fill scheduling.
    state.update(filled=1, remaining=2, held=1, cash=9010)
    partial, = broker.list_orders()
    assert (partial.status, partial.filled, partial.remaining) == ("partial", 1, 2)
    changed = broker.modify_order(placed.id, request(quantity=2, key="modify").model_copy(
        update={"price": Decimal(995)}), stop_condition_price=Decimal(0))
    assert state["price"] == 995 and state["remaining"] == 2
    broker.cancel_order(CancelRequest(order_id=changed.id, symbol="005930", quantity=2, client_key="cancel"))
    final, = broker.list_orders()
    assert (final.filled, final.remaining, state["cancelled"]) == (1, 0, 2)
    assert final.status == "unknown"  # The SDK does not invent cancellation confirmation semantics.
    assert broker.get_balance().cash_available == 9010
    held, = broker.get_positions()
    assert (held.quantity, held.sellable, held.market_value) == (1, 1, 990)
    check_requests([(host, path, headers, json.loads(body)) for host, path, headers, body in calls])


@pytest.mark.parametrize("value", [Decimal("1e1000"), Decimal("NaN"), Decimal("Infinity"), Decimal(-1)])
def test_modify_invalid_numeric_value_cannot_reach_transport(value, live_safety):
    broker, calls = make({}, safety=live_safety)
    with pytest.raises(ConfigurationError):
        broker.modify_order("mkt:101", request(), stop_condition_price=value)
    assert calls == []


def test_real_http_boundary_uses_official_token_query_not_form_body(monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    from pykbroker.brokers import nh

    sent = []

    class Reply:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"access_token":"synthetic-issued","expires_in":86400}'

    def capture(request, *, timeout):
        sent.append(request)
        return Reply()

    monkeypatch.setattr(nh, "urlopen", capture)
    broker = nh.NH(account="12345678901", client_options={
        "app_key": "synthetic key+", "app_secret": "synthetic secret&", "acct_type": "03",
    }, virtual=True)
    assert broker.issue_token()["token"] == "synthetic-issued"
    outgoing, = sent
    url = urlsplit(outgoing.full_url)
    assert url.scheme == "https" and url.netloc == "api.nhplug.com:8443" and url.path == "/oauth2/token"
    common = json.loads((Path(__file__).parent / "fixtures/nh/official/common-1024.json").read_text())
    parameters = common["paths"]["/oauth2/token"]["post"]["parameters"]
    assert {item["name"] for item in parameters if item["in"] == "query" and item["required"]} == set(
        parse_qs(url.query)
    )
    assert parse_qs(url.query) == {
        "appkey": ["synthetic key+"], "appsecretkey": ["synthetic secret&"],
        "grant_type": ["client_credentials"], "scope": ["oob"],
    }
    assert outgoing.data == b"" and outgoing.method == "POST"
    assert outgoing.get_header("Content-type") == "application/x-www-form-urlencoded"
