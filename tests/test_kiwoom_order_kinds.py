"""Kiwoom order kinds against the spec-derived contract server: market, conditional, best/top/mid, IOC/FOK,
off-session, NXT/SOR, amendment and cancel, several turns each. Mock (virtual) accounts are KRX only."""

from decimal import Decimal

import pytest

from pykbroker.exceptions import DuplicateOrderError, InvalidResponseError, NotSupportedError, TransportError
from pykbroker.models import AmendRequest, CancelRequest, OrderRequest

from .kiwoom_contract_server import SCHEMA, TRDE
from .test_kiwoom_contract_server import World


def live_world(**kw):
    world = World(**kw)
    broker = world.broker(virtual=False)
    broker.safety.market_order_allowed = lambda request: True
    broker.safety.valuation = lambda request: Decimal(request.quantity or 1) * Decimal(100_000)
    return world, broker


def req(key, **kw):
    base = {"symbol": "005930", "side": "buy", "quantity": 5, "client_key": key}
    return OrderRequest(**{**base, **kw})


CASES = [  # (request changes, expected trde_tp)
    ({"order_type": "limit", "price": "60000"}, "0"),
    ({"order_type": "limit", "price": "60000", "time_in_force": "ioc"}, "10"),
    ({"order_type": "limit", "price": "60000", "time_in_force": "fok"}, "20"),
    ({"order_type": "market"}, "3"),
    ({"order_type": "market", "time_in_force": "ioc"}, "13"),
    ({"order_type": "market", "time_in_force": "fok"}, "23"),
    ({"order_type": "conditional_limit", "price": "60000"}, "5"),
    ({"order_type": "best"}, "6"),
    ({"order_type": "best", "time_in_force": "ioc"}, "16"),
    ({"order_type": "best", "time_in_force": "fok"}, "26"),
    ({"order_type": "priority"}, "7"),
    ({"order_type": "midpoint"}, "29"),
    ({"order_type": "midpoint", "time_in_force": "ioc"}, "30"),
    ({"order_type": "midpoint", "time_in_force": "fok"}, "31"),
    ({"order_type": "close"}, "81"),
    ({"order_type": "market", "session": "pre"}, "61"),
    ({"order_type": "market", "session": "after"}, "81"),
    ({"order_type": "limit", "price": "60000", "session": "single_price"}, "62"),
]


@pytest.mark.parametrize("changes,code", CASES)
@pytest.mark.parametrize("venue", ["KRX", "NXT", "SOR"])
def test_every_listed_kind_reaches_the_server_with_its_official_trde_tp(changes, code, venue):
    world, broker = live_world()
    order = broker.place_order(req("k", venue=venue, **changes))
    assert world.server.invalid == []
    body = next(b for path, b in world.server.calls if b.get("ord_qty"))
    assert (body["trde_tp"], body["dmst_stex_tp"]) == (code, venue)
    assert order.status == "accepted" and order.venue == venue and order.client_key == "k"
    assert code in TRDE


def test_every_trde_tp_the_adapter_sends_is_in_the_official_list():
    official = SCHEMA["kt10000"]["request"]["trde_tp"]["description"]
    assert all(f"{code}:" in official for _, code in CASES)
    assert "28:스톱지정가" in official  # listed, deliberately not built (cond_uv meaning unstated)


def test_market_buy_nxt_amend_then_cancel_walk_with_partial_fill():
    world, broker = live_world()
    server = world.server
    # turn 1: NXT limit buy of 10, partial fill of 4
    order = broker.place_order(req("b1", quantity=10, venue="NXT", order_type="limit", price="60000"))
    server.fill(order.id, 4, 59_900)
    [row] = broker.list_orders("005930")
    assert (row.venue, row.status, row.filled, row.remaining) == ("NXT", "partial", 4, 6)
    # turn 2: amend the remaining 6 to a new price; answer carries a NEW number, the old one keeps its fills
    amended = broker.amend_order(AmendRequest(order_id=order.id, symbol="005930", side="buy", quantity=6,
                                              price="60500", venue="NXT", client_key="a1"))
    assert amended.status == "accepted"
    assert amended.id != order.id and amended.venue == "NXT" and amended.client_key == "a1"
    open_rows = {o.id: o for o in broker.list_orders("005930")}
    assert list(open_rows) == [amended.id] and open_rows[amended.id].remaining == 6
    amend_call = next(b for path, b in server.calls if "mdfy_qty" in b)
    assert amend_call["dmst_stex_tp"] == "NXT" and amend_call["mdfy_qty"] == "6"
    assert amend_call["mdfy_uv"] == "60500"
    # the original's ledger line is unknown after an amendment (adapter keeps its conservative rule)
    assert broker.status(order.model_copy(update={"venue": "NXT"})).status == "unknown"
    # turn 3: 2 more fill on the new order, cancel the last 4 on NXT
    server.fill(amended.id, 2)
    broker.cancel_order(CancelRequest(order_id=amended.id, symbol="005930", quantity=4, venue="NXT", client_key="c1"))
    assert broker.list_orders() == []
    done = broker.status(amended.model_copy(update={"venue": "NXT"}))
    assert (done.status, done.filled, done.remaining) == ("cancelled", 2, 0)
    # holdings: 4 + 2 shares
    assert broker.get_positions()[0].quantity == 6


def test_cancel_with_the_wrong_venue_and_unlisted_code_stays_unknown():
    world, broker = live_world()
    order = broker.place_order(req("k", venue="NXT", order_type="limit", price="60000"))
    with pytest.raises(InvalidResponseError):
        broker.cancel_order(CancelRequest(order_id=order.id, symbol="005930", quantity=5, client_key="c"))  # KRX
    assert world.server.orders[order.id]["remaining"] == 5


def test_market_order_fills_immediately_and_never_shows_as_open():
    world, broker = live_world()
    order = broker.place_order(req("m1", order_type="market"))
    assert broker.list_orders() == []
    assert broker.status(order).status == "filled"
    assert broker.get_positions()[0].quantity == 5


def test_ioc_remainder_and_fok_all_or_nothing():
    world, broker = live_world()
    world.server.liquidity = 3
    ioc = broker.place_order(req("i1", order_type="market", time_in_force="ioc"))
    assert world.server.orders[ioc.id]["cntr_qty"] == 3 and broker.list_orders() == []  # rest killed
    fok = broker.place_order(req("f1", order_type="market", time_in_force="fok"))
    assert world.server.orders[fok.id]["cntr_qty"] == 0 and broker.list_orders() == []
    assert broker.get_positions()[0].quantity == 3


def test_off_session_orders_rest_until_filled():
    world, broker = live_world()
    pre = broker.place_order(req("p1", order_type="market", session="pre"))
    single = broker.place_order(req("s1", order_type="limit", price="60000", session="single_price"))
    after = broker.place_order(req("c1", order_type="close"))
    assert {o.id for o in broker.list_orders()} == {pre.id, single.id, after.id}


def test_mock_account_takes_krx_only_and_the_wire_is_untouched():
    world = World()
    broker = world.broker()  # virtual
    broker.safety.market_order_allowed = lambda request: True
    broker.safety.valuation = lambda request: Decimal(100_000)
    before = len(world.server.calls)
    for venue in ("NXT", "SOR"):
        with pytest.raises(NotSupportedError):
            broker.place_order(req("k" + venue, venue=venue, order_type="market"))
    assert len(world.server.calls) == before
    assert broker.place_order(req("ok", order_type="market")).status == "accepted"


@pytest.mark.parametrize("kind", ["market", "best", "priority", "midpoint"])
def test_market_style_orders_need_session_authorization_before_any_request(kind):
    world = World()
    broker = world.broker(virtual=False)
    broker.safety.valuation = lambda request: Decimal(100_000)
    before = len(world.server.calls)
    with pytest.raises(Exception, match="market-order session authorization"):
        broker.place_order(req("k", order_type=kind))
    assert len(world.server.calls) == before


# --- amend_no_double_fill: a lost answer never leads to a second amendment or a new order ---


def test_amend_unknown_answer_is_not_resent_and_no_new_order_is_made():
    world, broker = live_world()
    order = broker.place_order(req("b1", order_type="limit", price="60000"))
    world.server.fill(order.id, 2, 60_000)
    calls_before = len(world.server.calls)
    real_post = world.server.post
    state = {"lost": True}

    def lossy(path, *, headers, json):
        result = real_post(path, headers=headers, json=json)  # the server DID process it
        if headers.get("api-id") == "kt10002" and state["lost"]:
            state["lost"] = False
            raise TimeoutError("answer lost")
        return result

    world.server.post = lossy
    amend = AmendRequest(order_id=order.id, symbol="005930", side="buy", quantity=3, price="60500", client_key="a1")
    with pytest.raises(TransportError):
        broker.amend_order(amend)
    # the same key is not re-sent; the server saw exactly one amendment, and only the amendment's replacement exists
    with pytest.raises(Exception, match="duplicate"):
        broker.amend_order(amend)
    sent = [b for _, b in world.server.calls[calls_before:] if "mdfy_qty" in b]
    assert len(sent) == 1
    assert len(world.server.orders) == 2  # original + the one replacement; no extra order
    # reconciliation by reading: the replacement is the open order, quantity not doubled
    [open_order] = broker.list_orders("005930")
    assert open_order.remaining == 3 and world.server.orders[order.id]["cntr_qty"] == 2


def test_amend_after_partial_fill_never_exceeds_the_remainder():
    world, broker = live_world()
    order = broker.place_order(req("b1", quantity=5, order_type="limit", price="60000"))
    world.server.fill(order.id, 3)
    too_big = AmendRequest(order_id=order.id, symbol="005930", side="buy", quantity=5, price="60500", client_key="a1")
    with pytest.raises(InvalidResponseError):
        broker.amend_order(too_big)  # only 2 remain: the server refuses, nothing is created
    assert len(world.server.orders) == 1
    ok = AmendRequest(order_id=order.id, symbol="005930", side="buy", quantity=2, price="60500", client_key="a2")
    assert broker.amend_order(ok).status == "accepted"
    assert sum(o["cntr_qty"] + o["remaining"] for o in world.server.orders.values()) == 5 + 2 - 2 + 0


def test_amending_a_market_original_is_refused_without_a_write():
    world, broker = live_world()
    world.server.liquidity = 0
    order = broker.place_order(req("m", order_type="market"))
    with pytest.raises(NotSupportedError):
        broker.amend_order(AmendRequest(order_id=order.id, symbol="005930", side="buy", quantity=5,
                                        price="60500", client_key="a"))
    assert not [b for _, b in world.server.calls if "mdfy_qty" in b]


# --- trde_tp 28 (스톱지정가): the slot exists, shut until Kiwoom confirms what cond_uv means ---


def stop_req(key="st"):
    return req(key, order_type="stop_limit", price="59000", stop_price="59500")


def test_stop_limit_is_listed_by_the_spec_but_refused_before_the_wire():
    world, broker = live_world()
    assert "stop_limit" not in broker.capabilities().order_kinds
    before = len(world.server.calls)
    with pytest.raises(NotSupportedError):
        broker.place_order(stop_req())
    assert len(world.server.calls) == before


def test_stop_limit_slot_builds_trde_tp_28_with_cond_uv_once_verified(monkeypatch):
    from pykbroker.brokers import kiwoom as module

    monkeypatch.setattr(module, "KIWOOM_STOP_LIMIT_VERIFIED", True)
    world, broker = live_world()
    assert "stop_limit" in broker.capabilities().order_kinds
    order = broker.place_order(stop_req())
    body = next(b for _, b in world.server.calls if b.get("ord_qty"))
    assert (body["trde_tp"], body["ord_uv"], body["cond_uv"]) == ("28", "59000", "59500")
    assert order.status == "accepted"


def test_stop_limit_request_shape_is_validated():
    with pytest.raises(ValueError):
        req("a", order_type="stop_limit", price="59000")  # no stop_price
    with pytest.raises(ValueError):
        req("b", order_type="limit", price="59000", stop_price="59500")  # stop_price on another kind


def test_us_amend_is_refused_before_any_http():
    world = World()
    broker = world.broker(virtual=False, market="US", venue="NASDAQ")
    broker.safety.valuation = lambda request: Decimal(100_000)
    before = list(world.server.calls)
    request = AmendRequest(order_id="1234567", symbol="AAPL", market="US", venue="NASDAQ",
                           side="buy", quantity=1, price="200", client_key="us-amend")
    with pytest.raises(NotSupportedError, match="Amend is not supported"):
        broker.amend_order(request)
    assert world.server.calls == before


def test_amend_same_order_number_is_unknown_and_cannot_be_resent(monkeypatch):
    world, broker = live_world()
    order = broker.place_order(req("original", quantity=1, order_type="limit", price="60000"))
    real_amend = world.server._kt10002

    def same_number(body, headers):
        response, response_headers = real_amend(body, headers)
        response["ord_no"] = body["orig_ord_no"]
        return response, response_headers

    monkeypatch.setattr(world.server, "_kt10002", same_number)
    request = AmendRequest(order_id=order.id, symbol="005930", side="buy", quantity=1,
                           price="60500", client_key="same-number")
    # InvalidResponseError means an unknown write outcome, never a confirmed refusal.
    with pytest.raises(InvalidResponseError, match="acknowledgement differs"):
        broker.amend_order(request)
    before_retry = list(world.server.calls)
    with pytest.raises(DuplicateOrderError):
        broker.amend_order(request)
    assert world.server.calls == before_retry
    assert len([body for _, body in world.server.calls if "mdfy_qty" in body]) == 1
    [replacement] = broker.list_orders("005930")
    assert replacement.id != order.id and replacement.remaining == 1
