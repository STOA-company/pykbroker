"""Multi-turn walks of the production LS adapter against the spec-derived contract server (no network).

Every order kind, venue and time-in-force the adapter offers is checked against the codes in the official TR
guide (tests/fixtures/ls/contract-20261024). Responses are fabricated; error codes are the synthetic ones the
server uses because the public guide has no code table (docs/ls-spec.md). Not LS sandbox or live evidence.
"""

from decimal import Decimal
from unittest.mock import Mock

import pytest

from pykbroker.brokers import ls as module
from pykbroker.brokers.ls import LS, LS_KR_EXTRA_ORDER_TYPES
from pykbroker.exceptions import (
    BrokerRejectedError,
    DuplicateOrderError,
    InvalidResponseError,
    NotSupportedError,
    SpecUnverifiedError,
    TransportError,
)
from pykbroker.models import AmendRequest, CancelRequest, OrderRequest
from pykbroker.safety.rate_limit import RateLimiter
from tests.ls_contract_server import PRICED_PTN, ContractServer


def order(**changes):
    return OrderRequest(**({"symbol": "005930", "side": "buy", "quantity": 3,
                           "price": "10000", "client_key": "walk-place"} | changes))


def amend(order_id, **changes):
    return AmendRequest(**({"order_id": order_id, "symbol": "005930", "side": "buy", "quantity": 2,
                           "price": "9900", "client_key": "walk-amend"} | changes))


def count(server, tr):
    return sum(call[1] == tr for call in server.calls)


@pytest.fixture
def world(monkeypatch, live_safety):
    server = ContractServer()
    monkeypatch.setattr(module, "urlopen", server.urlopen)
    live_safety.valuation = lambda request: Decimal("30000")
    live_safety.market_order_allowed = lambda request: True
    options = LS.issue_token(app_key="fixture-app-key", app_secret="fixture-app-secret", clock=server.clock)

    def build(**kwargs):
        return LS(account="fixture-account", client_options=options, allow_live_orders=True, safety=live_safety,
                  clock=server.clock, rate_limiter=kwargs.get("limiter", Mock(spec=RateLimiter)))

    return server, build()


KIND_CODE = {("regular", "limit"): "00", ("regular", "market"): "03",
             ("regular", "conditional_limit"): "05", ("regular", "best"): "06",
             ("regular", "priority"): "07", ("regular", "midpoint"): "12",
             ("pre", "market"): "61", ("after", "market"): "81", ("single_price", "limit"): "82"}


def test_kind_table_covers_every_code_in_the_official_guide():
    from tests.ls_contract_server import enum_codes

    official = enum_codes("CSPAT00601", "CSPAT00601InBlock1", "OrdprcPtnCode")
    assert set(KIND_CODE.values()) == official
    assert set(LS_KR_EXTRA_ORDER_TYPES) | {"limit", "market"} == {kind for session, kind in KIND_CODE}


@pytest.mark.parametrize("venue", ["KRX", "NXT"])
@pytest.mark.parametrize("session,kind", list(KIND_CODE))
def test_every_order_kind_and_venue_goes_out_with_its_documented_code(world, session, kind, venue):
    server, broker = world
    priced = KIND_CODE[session, kind] in PRICED_PTN
    placed = broker.place_order(order(order_type=kind, session=session, price="10000" if priced else None, venue=venue))
    body = server.placed[-1]
    assert (body["OrdprcPtnCode"], body["MbrNo"], body["OrdPrc"]) == (
        KIND_CODE[session, kind], venue, 10000 if priced else 0)
    assert placed.status == "accepted" and placed.order_type == kind
    assert broker.status(placed).status == "open"


@pytest.mark.parametrize("tif,code", [("day", "0"), ("ioc", "1"), ("fok", "2")])
def test_time_in_force_codes(world, tif, code):
    server, broker = world
    broker.place_order(order(time_in_force=tif))
    assert server.placed[-1]["OrdCndiTpCode"] == code


def test_unoffered_kinds_and_venues_never_reach_the_server(world):
    server, broker = world
    for bad in (order(order_type="close", price=None), order(venue="SOR"), order(price="70000.5")):
        with pytest.raises(Exception) as raised:
            broker.place_order(bad)
        assert isinstance(raised.value, (NotSupportedError, ValueError)) or "KR prices" in str(raised.value)
    assert count(server, "CSPAT00601") == 0


def test_buy_list_partial_fill_amend_cancel_balance_walk(world):
    server, broker = world
    caps = broker.capabilities()
    assert caps.supports_amend and caps.supports_nxt and caps.venues == ("KRX", "NXT")
    placed = broker.place_order(order())
    assert broker.status(placed).status == "open"
    assert placed.client_key == "walk-place"
    server.fill(placed.id, 1)
    partial = broker.status(placed)
    assert (partial.filled, partial.remaining, partial.status) == (1, 2, "partial")
    amended = broker.amend_order(amend(placed.id))
    assert amended.id != placed.id and amended.status == "accepted" and amended.side is None
    assert amended.client_key == "walk-amend"
    assert broker.status(amended).remaining == 2
    cancelled = broker.cancel_order(CancelRequest(order_id=amended.id, symbol="005930", quantity=2, client_key="walk-cancel"))
    assert cancelled.client_key == "walk-cancel"
    assert broker.status(amended).remaining == 0
    assert broker.status(placed).filled == 1
    balance = broker.get_balance()
    assert (balance.cash_available, balance.position_value) == (Decimal(900000 - 9950), Decimal(40000))
    [position] = broker.get_positions()
    assert (position.quantity, position.sellable, position.average_price) == (4, 4, Decimal("9912.5"))
    assert count(server, "CSPAT00601") == 1 and count(server, "CSPAT00701") == 1
    assert count(server, "CSPAT00801") == 1


def test_old_modify_name_is_the_same_call(world):
    server, broker = world
    placed = broker.place_order(order())
    old = broker.modify_order(placed.id, order(quantity=2, price="9900", client_key="old-name"))
    assert old.status == "accepted" and count(server, "CSPAT00701") == 1


def test_token_expiry_blocks_the_write_before_sending_and_a_reissue_sends_one_order(world, live_safety):
    server, broker = world
    server.advance(server.token_ttl)
    with pytest.raises(Exception, match="expired"):
        broker.place_order(order())
    assert count(server, "CSPAT00601") == 0
    options = LS.issue_token(app_key="fixture-app-key", app_secret="fixture-app-secret", clock=server.clock)
    fresh = LS(account="fixture-account", client_options=options, allow_live_orders=True, safety=live_safety,
               clock=server.clock, rate_limiter=Mock(spec=RateLimiter))
    fresh.place_order(order())
    assert count(server, "CSPAT00601") == 1 and len(server.placed) == 1


def test_server_side_token_refusal_is_unknown_and_the_order_is_not_resent(world):
    server, broker = world
    # LS answers a refused token with a code the guide never lists (fabricated here), on the write itself.
    server.reply_for["CSPAT00601"] = {"rsp_cd": "SYNTH_AUTH", "rsp_msg": "fabricated"}
    request = order()
    with pytest.raises(InvalidResponseError):
        broker.place_order(request)
    with pytest.raises(DuplicateOrderError):
        broker.place_order(request)
    assert count(server, "CSPAT00601") == 1 and server.placed == []


def test_token_the_gateway_forgot_stops_at_the_account_read_and_sends_no_order(world):
    server, broker = world
    server.tokens.clear()
    with pytest.raises(BrokerRejectedError):
        broker.place_order(order())
    assert count(server, "CSPAT00601") == 0


def test_call_limit_exceeded_is_unknown_never_success_and_never_resent(world, live_safety):
    server, broker = world
    live_safety.valuation = lambda request: Decimal(100)
    server.enforce_rates, server.rate_trs = True, {"CSPAT00601"}
    for index in range(10):
        broker.place_order(order(quantity=1, price="100", client_key=f"rate-{index}"))
    over = order(quantity=1, price="100", client_key="rate-over")
    with pytest.raises(InvalidResponseError):
        broker.place_order(over)
    with pytest.raises(DuplicateOrderError):
        broker.place_order(over)
    assert len(server.placed) == 10 and count(server, "CSPAT00601") == 11


def test_amend_no_double_fill_lost_answer_is_not_resent_and_no_new_order(world):
    server, broker = world
    placed = broker.place_order(order())
    server.after_write_failure = True  # the amend lands, the answer is lost
    with pytest.raises(TransportError):
        broker.amend_order(amend(placed.id))
    with pytest.raises(DuplicateOrderError):
        broker.amend_order(amend(placed.id))
    assert count(server, "CSPAT00701") == 1 and count(server, "CSPAT00601") == 1
    assert len(server.placed) == 1  # no second new order, whatever the answer was


def test_amend_no_double_fill_unknown_code_is_not_resent_and_no_new_order(world):
    server, broker = world
    placed = broker.place_order(order())
    server.reply_for["CSPAT00701"] = {"rsp_cd": "SYNTH_ORDER", "rsp_msg": "fabricated"}
    with pytest.raises(InvalidResponseError):
        broker.amend_order(amend(placed.id))
    with pytest.raises(DuplicateOrderError):
        broker.amend_order(amend(placed.id))
    assert count(server, "CSPAT00701") == 1 and len(server.placed) == 1


def test_market_amend_and_locked_amend_are_refused_before_any_write(world, monkeypatch):
    server, broker = world
    placed = broker.place_order(order())
    with pytest.raises(NotSupportedError):
        broker.amend_order(order(order_type="market", price=None, client_key="m"), order_id=placed.id)
    assert count(server, "CSPAT00701") == 0
    locked = LS(account="fixture-account", client=Mock(base_url="https://openapi.ls-sec.co.kr:8080"))
    assert locked.capabilities().supports_amend is False
    with pytest.raises(SpecUnverifiedError):
        locked.amend_order(amend("101"))


@pytest.mark.parametrize("changes", [{"time_in_force": "ioc"}, {"time_in_force": "fok"},
                                      {"session": "single_price"}])
def test_amend_conditions_not_declared_by_adapter_are_rejected_before_http(world, changes):
    server, broker = world
    before = len(server.calls)
    with pytest.raises(NotSupportedError):
        broker.amend_order(amend("101", **changes))
    assert len(server.calls) == before


def test_fills_update_cash_holdings_and_cost_through_amend_and_sell(world):
    server, broker = world
    initial = broker.get_balance()
    [initial_position] = broker.get_positions()
    placed = broker.place_order(order())
    assert placed.client_key == "walk-place"
    server.fill(placed.id, 1, 9960)
    amended = broker.amend_order(amend(placed.id))
    server.fill(amended.id, 2, 9900)
    after_buy = broker.get_balance()
    [held] = broker.get_positions()
    cost = Decimal(9960 + 2 * 9900)
    assert after_buy.cash_available == initial.cash_available - cost
    assert (held.quantity, held.sellable) == (initial_position.quantity + 3, initial_position.sellable + 3)
    assert held.average_price == (Decimal(29700) + cost) / 6
    sell = broker.place_order(order(side="sell", quantity=2, price="10100", client_key="sell"))
    server.fill(sell.id, 2, 10100)
    assert broker.get_balance().cash_available == initial.cash_available - cost + 20200
    [sold] = broker.get_positions()
    assert (sold.quantity, sold.sellable) == (held.quantity - 2, held.sellable - 2)
    assert sold.average_price == held.average_price
