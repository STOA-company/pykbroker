"""Meritz adapter against the ported official-contract fake server (tests/meritz_contract_server.py).

The server validates every request body/query against the official OpenAPI 3.1 schema (the 19 paths in
tests/fixtures/meritz/official/openapi.json, identical to the 2026-10-28 spec), so a wrong field, type or length in
this adapter fails here. It is an offline contract walk, not a claim about the live server.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pykbroker.brokers import meritz as module
from pykbroker.brokers.meritz import Meritz
from pykbroker.exceptions import (
    BrokerRejectedError,
    DuplicateOrderError,
    InvalidResponseError,
    TransportError,
)
from pykbroker.models import CancelRequest, Order, OrderRequest
from pykbroker.safety.rate_limit import RateLimiter

from .meritz_contract_server import ContractServer

NOW = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
OPTIONS = {"app_key": "fixture-key", "app_secret": "fixture-secret", "mac_address": "001122AABBCC"}


class WithStatus:
    """The ported server answers a body; the adapter's boundary returns (HTTP status, body). Vendor errors are 500."""

    def __init__(self, server):
        self.server = server
        self.base_url = server.base_url

    def request(self, method, path, **kwargs):
        body = self.server.request(method, path, **kwargs)
        failed = (
            isinstance(body, dict) and "access_token" not in body and body.get("rsp_cd") not in module.SUCCESS_CODES
        )
        return (500 if failed else 200), body


@pytest.fixture
def world(live_safety, monkeypatch):
    monkeypatch.setattr(module, "MERITZ_SPEC_VERIFIED", True)
    monkeypatch.setattr(RateLimiter, "wait", lambda *args: None)
    module._TOKENS.clear()
    live_safety.valuation = lambda request: Decimal(1000)

    def make(market="KR", venue=None, allow=True):
        server = ContractServer()
        broker = Meritz(
            account="fixture-account",
            client_options=OPTIONS,
            client_factory=lambda **kw: WithStatus(server),
            allow_live_orders=allow,
            market=market,
            venue=venue,
            safety=live_safety,
            clock=lambda: NOW,
            sleeper=lambda seconds: None,
        )
        return server, broker

    yield make
    module._TOKENS.clear()


def order(market="KR", **kw):
    base = {
        "symbol": "005930" if market == "KR" else "BRK.B",
        "market": market,
        "side": "buy",
        "quantity": 3,
        "price": "1000",
        "client_key": "place",
        "venue": "KRX" if market == "KR" else "NYSE",
    }
    return OrderRequest(**(base | kw))


def cancel(placed, **kw):
    base = {
        "order_id": placed.id,
        "symbol": placed.symbol,
        "market": placed.market,
        "quantity": 2,
        "client_key": "cancel",
        "venue": "KRX" if placed.market == "KR" else "NYSE",
    }
    return CancelRequest(**(base | kw))


MARKETS = [("KR", None), ("US", "NYSE")]


@pytest.mark.parametrize("market,venue", MARKETS)
def test_place_partial_fill_cancel_rest_and_balance(world, market, venue):
    server, broker = world(market, venue)
    placed = broker.place_order(order(market))
    assert placed.status == "accepted"
    assert broker.status(placed).status == "open"
    server.fill(market, placed.id, 1, 1000)
    got = broker.status(placed)
    assert (got.status, got.filled, got.remaining) == ("partial", 1, 2)
    broker.cancel_order(cancel(placed))
    done = broker.status(placed)
    assert (done.status, done.filled, done.remaining) == ("cancelled", 1, 0)
    assert [p.quantity for p in broker.get_positions()] == [1]
    posted = [c for c in server.calls if c[0] == "POST" and c[1].rsplit("/", 1)[-1] in {"buy", "sell"}]
    assert len(posted) == 1  # the order itself went out once


@pytest.mark.parametrize("market,venue", MARKETS)
def test_full_fill_is_read_only_from_the_ledger(world, market, venue):
    server, broker = world(market, venue)
    placed = broker.place_order(order(market, quantity=2))
    assert broker.status(placed).status == "open"  # the acknowledgement is not a fill
    server.fill(market, placed.id, 2, 1000)
    got = broker.status(placed)
    assert (got.status, got.filled, got.remaining) == ("filled", 2, 0)
    assert broker.get_balance().cash_available == Decimal(100000 if market == "KR" else 10000) - 2000


@pytest.mark.parametrize("market,venue", MARKETS)
def test_warning_is_not_acceptance_and_is_never_confirmed_automatically(world, market, venue):
    server, broker = world(market, venue)
    server.warning = "1"
    with pytest.raises(BrokerRejectedError) as caught:
        broker.place_order(order(market))
    assert caught.value.reason == "meritz_warning"
    assert [c for c in server.calls if c[0] == "POST" and c[1].endswith("/buy")].__len__() == 1


@pytest.mark.parametrize("market,venue", MARKETS)
def test_lost_acknowledgement_is_unknown_and_never_resent(world, market, venue):
    server, broker = world(market, venue)
    server.lose_next_write = True
    with pytest.raises(TransportError):
        broker.place_order(order(market))
    with pytest.raises(DuplicateOrderError):
        broker.place_order(order(market))
    assert len([c for c in server.calls if c[0] == "POST" and c[1].endswith("/buy")]) == 1
    # the order exists at the venue: the ledger, not the lost answer, settles it
    listed = broker.list_orders()
    assert len(listed) == 1 and listed[0].status == "open"


@pytest.mark.parametrize("code,kind", [("0760", "rejected"), ("EGW00103", "rejected"), ("EGW00200", "rate_limited")])
def test_known_refusals_on_a_write_are_definite_and_single(world, code, kind):
    server, broker = world()
    server.write_failure = code
    with pytest.raises(BrokerRejectedError) as caught:
        broker.place_order(order())
    assert caught.value.kind == kind
    assert len([c for c in server.calls if c[0] == "POST" and c[1].endswith("/buy")]) == 1


def test_token_expired_at_the_write_is_one_post_and_the_next_order_reissues(world):
    server, broker = world()
    server.write_failure = "EGW00123"
    with pytest.raises(BrokerRejectedError) as caught:
        broker.place_order(order(client_key="a"))
    assert caught.value.kind == "token_expired"
    assert broker.place_order(order(client_key="b")).status == "accepted"
    assert len([c for c in server.calls if c[1] == "/oauth2/token"]) == 2


def test_server_error_on_a_write_is_unknown(world):
    server, broker = world()
    server.write_failure = "EGW00002"
    with pytest.raises(InvalidResponseError):
        broker.place_order(order())


def test_read_throttle_and_expiry_recover_without_a_second_order(world):
    server, broker = world()
    server.failure = "EGW00200"
    assert broker.get_balance().currency == "KRW"
    server.failure = "EGW00123"
    assert broker.get_balance().currency == "KRW"
    placed = broker.place_order(order())
    assert placed.status == "accepted"
    assert len([c for c in server.calls if c[0] == "POST" and c[1].endswith("/buy")]) == 1


def test_kr_modify_then_the_chain_is_unknown_not_guessed(world):
    server, broker = world("KR")
    placed = broker.place_order(order("KR"))
    changed = broker.modify_order(placed.id, order("KR", quantity=2, price="1100", client_key="modify"))
    assert changed.id != placed.id and changed.status == "accepted"
    assert broker.status(placed).status == "unknown"  # KR change rows all point at the first order


def test_us_modify_then_cancel_names_the_latest_number(world):
    server, broker = world("US", "NYSE")
    placed = broker.place_order(order("US"))
    changed = broker.modify_order(placed.id, order("US", quantity=2, price="1100", client_key="modify"))
    assert broker.status(placed).status == "unknown" and broker.status(changed).status == "open"
    with pytest.raises(BrokerRejectedError):  # the old number is stale
        broker.cancel_order(cancel(placed, quantity=2, client_key="stale"))
    broker.cancel_order(cancel(changed, quantity=2, client_key="latest"))
    assert broker.status(changed).status == "cancelled"


def test_pagination_keeps_trailing_spaces_and_a_cut_page_is_refused(world):
    server, broker = world()
    server.page_size = 1
    first = broker.place_order(order(client_key="p1"))
    second = broker.place_order(order(client_key="p2"))
    assert {o.id for o in broker.list_orders()} == {first.id, second.id}
    keys = [
        c[2].get("tr_cont_key")
        for c in server.calls
        if c[1] == "/trading/v1/orders/history" and c[2].get("tr_cont_key")
    ]
    assert keys and all(k.endswith("  ") for k in keys)
    server.overrides["/trading/v1/orders/history"] = {"rsp_cd": "5762", "tr_cont": "1", "data": []}
    with pytest.raises(InvalidResponseError):
        broker.list_orders()


def test_no_data_code_does_not_discard_data(world):
    server, broker = world()
    placed = broker.place_order(order(quantity=1))
    server.fill("KR", placed.id, 1, 1000)
    held = server.request(
        "GET",
        "/accounts/v1/me/holdings",
        headers={"mac_address": "001122AABBCC", "authorization": "Bearer fixture-token"},
        params={},
    )
    held["rsp_cd"] = "5820"
    server.overrides["/accounts/v1/me/holdings"] = held
    assert [p.quantity for p in broker.get_positions()] == [1]  # data decides, not the 5820


def test_a_status_of_an_order_the_ledger_does_not_show_is_unknown(world):
    server, broker = world()
    assert broker.status(Order(id="999", symbol="005930", status="accepted")).status == "unknown"


def test_writes_stay_locked_without_both_switches(world, monkeypatch):
    server, broker = world(allow=False)
    from pykbroker.exceptions import NotSupportedError

    with pytest.raises(NotSupportedError):
        broker.place_order(order())
    assert not [c for c in server.calls if c[0] == "POST" and c[1].endswith("/buy")]
