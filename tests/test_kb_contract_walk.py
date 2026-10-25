"""Multi-turn walks of the production KB adapter against the spec-derived in-process server (no network).

Order kinds, venues and request codes are checked against the official XLSX input tables (SSAM1801/1802/1805).
Where KB puts an error code in the response is not documented, so error envelopes are synthetic
(docs/kb-spec.md). Not a KB sandbox or live run.
"""

from decimal import Decimal

import pytest

from pykbroker.brokers.kb import KB, KB_KR_ORDER_CCD, KB_KR_VENUE_CODE
from pykbroker.exceptions import (
    BrokerRejectedError,
    DuplicateOrderError,
    InvalidResponseError,
    NotSupportedError,
    TransportError,
)
from pykbroker.models import AmendRequest
from tests.test_kb_official_contract import ContractServer, cancel, contract_broker, no_wait, order  # noqa: F401


def amend(order_id, **changes):
    return AmendRequest(**({"order_id": order_id, "symbol": "005930", "side": "buy", "quantity": 1,
                           "price": Decimal("1100"), "client_key": "walk-amend"} | changes))


def count(server, path):
    return sum(call[0] == "/api/v1/" + path for call in server.calls)


@pytest.fixture
def world(live_safety):
    live_safety.market_order_allowed = lambda request: True
    live_safety.valuation = lambda request: Decimal(1000)
    server = ContractServer()
    return server, contract_broker(server, live_safety)


def test_kind_table_matches_the_official_enum():
    assert set(KB_KR_ORDER_CCD.values()) == {"00", "03", "12", "13", "M3"}
    assert KB_KR_VENUE_CODE == {"KRX": "K", "NXT": "N", "SOR": "S"}


@pytest.mark.parametrize("venue", ["KRX", "NXT", "SOR"])
@pytest.mark.parametrize("side,tr", [("buy", "ssam1802"), ("sell", "ssam1801")])
def test_limit_order_and_venue_go_out_with_the_documented_code(world, venue, side, tr):
    server, broker = world
    placed = broker.place_order(order(side=side, order_type="limit", venue=venue, price=Decimal("1000")))
    body = server.placed[-1]
    assert (body["ordr_ccd"], body["sor_ordr_ccd"], body["ordr_uprc"]) == (
        "00", KB_KR_VENUE_CODE[venue], "1000")
    assert count(server, tr) == 1 and placed.status == "accepted" and placed.order_type == "limit"


def test_kinds_the_api_lists_only_in_its_output_enum_are_not_sent(world):
    server, broker = world
    for changes in ({"order_type": "conditional_limit"}, {"session": "pre"},
                    {"session": "after"}, {"session": "single_price"}):
        with pytest.raises(NotSupportedError):
            broker.place_order(order(**changes))
    with pytest.raises(NotSupportedError):
        broker.place_order(order(time_in_force="ioc"))
    assert server.placed == []


def test_buy_list_partial_fill_amend_cancel_walk(world):
    server, broker = world
    caps = broker.capabilities()
    assert caps.supports_amend and caps.supports_nxt and not caps.supports_market_order
    assert caps.order_kinds == ("limit",)
    placed = broker.place_order(order(quantity=2, price=Decimal("1000")))
    server.fill(placed.id, 1)
    [partial] = broker.list_orders()
    assert (partial.filled, partial.remaining, partial.status) == (1, 1, "partial")
    amended = broker.amend_order(amend(placed.id))
    assert amended.client_key == "walk-amend"
    assert amended.status == "accepted" and amended.side is None and amended.id != placed.id
    cancelled = broker.cancel_order(cancel(order_id=amended.id))
    assert cancelled.status == "unknown" and cancelled.client_key == "fixture-cancel"
    rows = {row.id: row for row in broker.list_orders()}
    assert rows[amended.id].remaining == 0 and rows[placed.id].filled == 1
    assert [count(server, name) for name in ("ssam1802", "ssam1805", "ssam1806")] == [1, 1, 1]


def test_old_amend_shape_is_the_same_call(world):
    server, broker = world
    placed = broker.place_order(order(quantity=2, price=Decimal("1000")))
    old = broker.amend_order(order(price=Decimal("1100"), client_key="old"), order_id=placed.id)
    assert old.status == "accepted" and count(server, "ssam1805") == 1


def test_token_expiry_mid_walk_rejects_the_write_once_and_never_resends(world, live_safety):
    server, broker = world
    server.now = server.expiry
    request = order(price=Decimal("1000"))
    with pytest.raises(BrokerRejectedError) as raised:
        broker.place_order(request)
    assert (raised.value.kind, raised.value.code) == ("token_expired", "T043")
    with pytest.raises(DuplicateOrderError):
        broker.place_order(request)
    assert count(server, "ssam1802") == 1 and server.placed == [] and server.token_count == 1
    reissued = contract_broker(server, live_safety)
    server.now = 0
    reissued.place_order(order(price=Decimal("1000"), client_key="after-reissue"))
    assert len(server.placed) == 1 and server.token_count == 2


def test_call_limit_is_a_rate_limited_refusal_and_never_resent(world):
    server, broker = world
    server.error_for["/api/v1/ssam1802"] = "E041"
    request = order(price=Decimal("1000"))
    with pytest.raises(BrokerRejectedError) as raised:
        broker.place_order(request)
    assert raised.value.kind == "rate_limited"
    with pytest.raises(DuplicateOrderError):
        broker.place_order(request)
    assert count(server, "ssam1802") == 1 and server.placed == []


def test_amend_no_double_fill_lost_answer_is_not_resent_and_no_new_order(world):
    server, broker = world
    placed = broker.place_order(order(quantity=2, price=Decimal("1000")))
    server.lose_next_write = True
    with pytest.raises(TransportError):
        broker.amend_order(amend(placed.id))
    with pytest.raises(DuplicateOrderError):
        broker.amend_order(amend(placed.id))
    assert count(server, "ssam1805") == 1 and count(server, "ssam1802") == 1 and len(server.placed) == 1


def test_amend_no_double_fill_unknown_code_is_not_resent_and_no_new_order(world):
    server, broker = world
    placed = broker.place_order(order(quantity=2, price=Decimal("1000")))
    server.error_for["/api/v1/ssam1805"] = "T022"  # listed in the public list but not a sourced reject row
    with pytest.raises(InvalidResponseError):
        broker.amend_order(amend(placed.id))
    with pytest.raises(DuplicateOrderError):
        broker.amend_order(amend(placed.id))
    assert count(server, "ssam1805") == 1 and len(server.placed) == 1


def test_us_has_no_amend_here_and_refuses_before_any_call():
    server = ContractServer()
    us = KB(account="fixture-account", bound_account="fixture-account", app_key="k" * 36, token="t",
            client=server, market="US", venue="NASDAQ", is_integrated=True, writes_enabled=True)
    assert us.capabilities().supports_amend is False
    with pytest.raises(NotSupportedError):
        us.amend_order(amend("9000000001", symbol="TSLA", venue="NASDAQ", market="US"))
    assert server.calls == []


@pytest.mark.parametrize("action,tr", [("place", "ssam1802"), ("amend", "ssam1805"), ("cancel", "ssam1806")])
def test_token_expired_response_discards_stored_token_and_next_read_sends_nothing(world, action, tr):
    from pykbroker.exceptions import ConfigurationError
    server, broker = world
    placed = broker.place_order(order(quantity=2, price=Decimal("1000")))
    server.error_for["/api/v1/" + tr] = "T043"
    calls = {
        "place": lambda: broker.place_order(order(price=Decimal("1000"), client_key="expired")),
        "amend": lambda: broker.amend_order(amend(placed.id)),
        "cancel": lambda: broker.cancel_order(cancel(order_id=placed.id)),
    }
    with pytest.raises(BrokerRejectedError) as raised:
        calls[action]()
    assert raised.value.kind == "token_expired" and broker._token is None
    before = len(server.calls)
    with pytest.raises(ConfigurationError):
        broker.get_balance()
    assert len(server.calls) == before


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("kind,price,stop", [
    ("market", None, None), ("best", None, None), ("priority", None, None),
    ("midpoint", None, None), ("close", None, None),
    ("conditional_limit", Decimal("1000"), None), ("stop_limit", Decimal("1000"), Decimal("1100")),
])
def test_undeclared_kr_kind_is_rejected_before_any_transport(world, side, kind, price, stop):
    server, broker = world
    before = len(server.calls)
    with pytest.raises(NotSupportedError):
        broker.place_order(order(side=side, order_type=kind, price=price, stop_price=stop))
    assert len(server.calls) == before
    assert server.placed == []
