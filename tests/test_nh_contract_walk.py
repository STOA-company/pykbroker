"""Multi-turn walks of the production NH adapter against the spec-derived stateful server (no network).

Not NH sandbox or live evidence: error-code placement, ledger rows after a modify, and the mkt/itg order-number
link are the server's assumptions (docs/nh-spec.md).
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pykbroker.brokers.nh import NH, NH_KR_PRICE_TYPE, NH_KR_TIF
from pykbroker.exceptions import (
    BrokerRejectedError,
    DuplicateOrderError,
    InvalidResponseError,
    NotSupportedError,
    TransportError,
)
from pykbroker.models import AmendRequest, CancelRequest, OrderRequest
from pykbroker.safety.rate_limit import RateLimiter
from tests.nh_contract_server import ORDER, NHContractServer, description_codes

NOW = datetime(2026, 10, 26, 0, 30, tzinfo=UTC)


def build(server, live_safety, *, virtual=True, venue="KRX"):
    return NH(account="12345678901",
              client_options={"app_key": "fixture-key", "app_secret": "fixture-secret", "token": "fixture-token",
                              "acct_type": "03" if virtual else "01"},
              client_factory=server.factory, allow_live_orders=True, clock=lambda: NOW,
              rate_limiter=RateLimiter({"read": 10000}), safety=live_safety, virtual=virtual, venue=venue)


@pytest.fixture
def world(live_safety):
    live_safety.market_order_allowed = lambda request: True
    live_safety.valuation = lambda request: Decimal(3000)
    server = NHContractServer()
    return server, build(server, live_safety)


def req(kind="limit", side="buy", qty=3, key="walk-place", **changes):
    priced = kind in {"limit", "conditional_limit"}
    return OrderRequest(symbol="005930", side=side, quantity=qty, order_type=kind,
                        price=Decimal(1000) if priced else None, client_key=key, **changes)


def amend_req(order_id, **changes):
    return AmendRequest(**({"order_id": order_id, "symbol": "005930", "side": "buy", "quantity": 2,
                           "price": Decimal(995), "client_key": "walk-amend",
                           "broker_ref": {"original_ledger_id": "itg:9001", "stop_condition_price": "0"}} | changes))


def count(server, path):
    return sum(call[0] == path for call in server.calls)


def test_kind_and_tif_tables_cover_the_official_codes_the_adapter_offers():
    official = description_codes(ORDER + "cashBuy", "nmn_pr_tp_cd")
    assert set(NH_KR_PRICE_TYPE.values()) <= set(official)
    assert {"16", "09", "10", "11", "91", "92"}.isdisjoint(NH_KR_PRICE_TYPE.values())  # documented non-offers
    assert set(NH_KR_TIF.values()) <= set(description_codes(ORDER + "cashBuy", "orr_cnd_dit_cd"))


@pytest.mark.parametrize("venue", ["KRX", "NXT"])
@pytest.mark.parametrize("session,kind,code",
    [("regular", kind, code) for kind, code in NH_KR_PRICE_TYPE.items()] +
    [("pre", "market", "61"), ("after", "market", "71")])
@pytest.mark.parametrize("side,endpoint", [("buy", "cashBuy"), ("sell", "cashSell")])
def test_every_order_kind_and_venue_goes_out_with_its_documented_code(live_safety, session, kind, code, venue, side, endpoint):
    live_safety.market_order_allowed = lambda request: True
    live_safety.valuation = lambda request: Decimal(3000)
    server = NHContractServer()
    broker = build(server, live_safety, venue=venue)
    placed = broker.place_order(req(kind, side, session=session))
    wire = server.placed[-1]
    assert wire["nmn_pr_tp_cd"] == code and wire["rmt_mkt_cd"] == venue
    assert count(server, ORDER + endpoint) == 1 and placed.status == "accepted" and placed.order_type == kind
    [row] = broker.list_orders()
    assert row.status == "open" and row.order_type == kind


@pytest.mark.parametrize("tif", ["day", "ioc", "fok"])
def test_time_in_force_codes(world, tif):
    server, broker = world
    broker.place_order(req(time_in_force=tif))
    assert server.placed[-1]["orr_cnd_dit_cd"] == NH_KR_TIF[tif]


def test_unoffered_kinds_never_reach_the_server(world):
    server, broker = world
    with pytest.raises(NotSupportedError):
        broker.place_order(OrderRequest(symbol="005930", side="buy", quantity=1, order_type="close",
                                        client_key="c"))
    with pytest.raises(NotSupportedError):
        broker.place_order(OrderRequest(symbol="005930", side="buy", quantity=1, order_type="limit", session="single_price",
                                        price=Decimal(1000), client_key="s"))
    assert server.placed == []


def test_buy_partial_fill_amend_cancel_balance_walk(world):
    server, broker = world
    caps = broker.capabilities()
    assert caps.supports_amend and "midpoint" in caps.order_kinds and caps.time_in_force == ("day", "ioc", "fok")
    cash0 = broker.get_balance().cash_available
    placed = broker.place_order(req())
    assert placed.id == "mkt:101" and placed.client_key == "walk-place"
    server.fill(101, 1, 990)
    [partial] = broker.list_orders()
    assert (partial.status, partial.filled, partial.remaining) == ("partial", 1, 2)
    amended = broker.amend_order(amend_req(placed.id))
    assert amended.client_key == "walk-amend"
    assert amended.id == "mkt:102" and server.live(102)["price"] == 995
    cancelled = broker.cancel_order(CancelRequest(order_id=amended.id, symbol="005930", quantity=2, client_key="walk-cancel"))
    assert cancelled.client_key == "walk-cancel"
    [final] = broker.list_orders()
    assert (final.filled, final.remaining, server.live(102)["cancelled"]) == (1, 0, 2)
    assert final.status == "unknown"  # a cancel is never promoted to a confirmed state by the SDK
    assert broker.get_balance().cash_available == cash0 - 990
    [held] = broker.get_positions()
    assert (held.quantity, held.sellable) == (1, 1)
    assert [count(server, ORDER + n) for n in ("cashBuy", "modify", "cancel")] == [1, 1, 1]


def test_old_modify_name_is_the_same_call(world):
    server, broker = world
    placed = broker.place_order(req())
    changed = broker.modify_order(placed.id, req(qty=2, key="old").model_copy(update={"price": Decimal(995)}),
                                  stop_condition_price=Decimal(0), original_ledger_id="itg:9001")
    assert changed.status == "accepted" and count(server, ORDER + "modify") == 1


def test_token_expiry_rejects_the_write_discards_the_token_and_never_resends(world):
    server, broker = world
    server.token_ok = False
    request = req()
    with pytest.raises(BrokerRejectedError) as raised:
        broker.place_order(request)
    assert (raised.value.kind, raised.value.code) == ("token_expired", "IGW40044")
    assert broker.token_state()["token"] == ""
    with pytest.raises(DuplicateOrderError):
        broker.place_order(request)
    assert count(server, ORDER + "cashBuy") == 1 and server.placed == []


def test_call_limit_is_a_rate_limited_refusal_and_never_resent(world):
    server, broker = world
    server.write_limit = 0
    request = req()
    with pytest.raises(BrokerRejectedError) as raised:
        broker.place_order(request)
    assert raised.value.kind == "rate_limited"
    with pytest.raises(DuplicateOrderError):
        broker.place_order(request)
    assert count(server, ORDER + "cashBuy") == 1 and server.placed == []


def test_amend_no_double_fill_lost_answer_is_not_resent_and_no_new_order(world):
    server, broker = world
    placed = broker.place_order(req())
    server.lose_next_write = True
    with pytest.raises(TransportError):
        broker.amend_order(amend_req(placed.id))
    with pytest.raises(DuplicateOrderError):
        broker.amend_order(amend_req(placed.id))
    assert count(server, ORDER + "modify") == 1 and count(server, ORDER + "cashBuy") == 1


@pytest.mark.parametrize("answer", [("http", 500), ("code", "IGW99999")])
def test_amend_no_double_fill_unknown_answer_is_not_resent_and_no_new_order(world, answer):
    server, broker = world
    placed = broker.place_order(req())
    if answer[0] == "http":
        server.status_next_write = answer[1]
        expected = TransportError
    else:
        server.error_for[ORDER + "modify"] = answer[1]  # not a sourced row: stays unknown
        expected = InvalidResponseError
    with pytest.raises(expected):
        broker.amend_order(amend_req(placed.id))
    with pytest.raises(DuplicateOrderError):
        broker.amend_order(amend_req(placed.id))
    assert count(server, ORDER + "modify") == 1 and len(server.placed) == 1


def test_amend_without_caller_ledger_assertion_and_unsupported_venue_send_nothing(world, live_safety):
    server, broker = world
    placed = broker.place_order(req())
    bare = amend_req(placed.id).model_copy(update={"broker_ref": {}})
    with pytest.raises(Exception, match="broker_ref"):
        broker.amend_order(bare)
    unt = build(server, live_safety, venue="UNT")
    assert unt.capabilities().supports_amend is False
    with pytest.raises(NotSupportedError):
        unt.amend_order(amend_req(placed.id))
    assert count(server, ORDER + "modify") == 0


@pytest.mark.parametrize("changes", [{"time_in_force": "ioc"}, {"time_in_force": "fok"},
                                      {"session": "single_price"}])
def test_amend_conditions_not_declared_by_adapter_are_rejected_before_http(world, changes):
    server, broker = world
    with pytest.raises(NotSupportedError):
        broker.amend_order(amend_req("mkt:101", **changes))
    assert server.calls == []


def test_401_without_code_after_acceptance_is_unknown_discards_token_and_keeps_key(world):
    server, broker = world
    server.status_next_write = 401
    request = req()
    with pytest.raises(TransportError):
        broker.place_order(request)
    assert broker._token is None
    with pytest.raises(DuplicateOrderError):
        broker.place_order(request)
    assert server.writes == 1 and len(server.placed) == 1
