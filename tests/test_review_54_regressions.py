"""Review #54 regressions at the real adapter boundary; fake transports only."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pykbroker.brokers.kis import KIS
from pykbroker.brokers.toss import Toss
from pykbroker.exceptions import BrokerRejectedError, ConfigurationError, DuplicateOrderError, InvalidResponseError, NotSupportedError
from pykbroker.models import AmendRequest, CancelRequest, OrderRequest

from .test_kiwoom_contract_server import World, buy
from .test_toss_contract import LoopSDK


@pytest.mark.parametrize("code", [1, 20, -1, 1998, 2000])
def test_unlisted_integer_after_acceptance_stays_unknown_and_key_locked(code, monkeypatch):
    world = World()
    broker = world.broker()
    original = world.server._kt10000
    def accept_then_lose_answer(body, headers):
        original(body, headers)
        return {"return_code": code}, {"cont-yn": "N"}
    monkeypatch.setattr(world.server, "_kt10000", accept_then_lose_answer)
    request = buy("uncertain", qty=1)
    with pytest.raises(InvalidResponseError):
        broker.place_order(request)
    with pytest.raises(DuplicateOrderError):
        broker.place_order(request)
    assert len(world.server.orders) == 1
    assert sum(path == "/api/dostk/ordr" for path, _ in world.server.calls) == 1


@pytest.mark.parametrize("condition", [{"time_in_force": "ioc"}, {"time_in_force": "fok"},
                                        {"session": "single_price"}, {"broker_ref": {"unused": "value"}}])
def test_kiwoom_unsupported_amend_condition_is_rejected_before_any_http(condition):
    world = World()
    broker = world.broker()
    before = len(world.server.calls)
    with pytest.raises(NotSupportedError):
        broker.amend_order(AmendRequest(**{**buy("amend").model_dump(), "order_id": "42", **condition}))
    assert len(world.server.calls) == before


@pytest.mark.parametrize("action", ["place", "cancel", "amend"])
def test_expired_kiwoom_token_is_discarded_without_resending(action):
    world = World()
    broker = world.broker()
    order = broker.place_order(buy("initial", qty=1))
    requests = {
        "place": ("kt10000", lambda: broker.place_order(buy("expired", qty=1))),
        "cancel": ("kt10003", lambda: broker.cancel_order(CancelRequest(order_id=order.id, symbol="005930", quantity=1, client_key="expired"))),
        "amend": ("kt10002", lambda: broker.amend_order(AmendRequest(**buy("expired", qty=1).model_dump(), order_id=order.id))),
    }
    tr, call = requests[action]
    world.server.fail_next[tr] = (8005, "")
    with pytest.raises(BrokerRejectedError) as raised:
        call()
    assert raised.value.kind == "token_expired"
    assert broker._authorization is None
    before = len(world.server.calls)
    with pytest.raises(ConfigurationError):
        broker.available_cash()
    assert len(world.server.calls) == before


def test_toss_kr_market_capability_reaches_submit_and_echoes_client_key(live_safety):
    made = []
    def factory(**kwargs):
        made.append(LoopSDK(**kwargs))
        return made[-1]
    live_safety.market_order_allowed = lambda request: True
    live_safety.valuation = lambda request: Decimal("70000")
    with Toss(account="fixture", account_seq=1, sdk_factory=factory, sdk_dry_run=False, safety=live_safety) as broker:
        assert broker.capabilities().order_kinds == ("limit", "market")
        order = broker.place_order(OrderRequest(symbol="005930", side="buy", quantity=1, order_type="market", client_key="market-key"))
        assert order.client_key == "market-key"
        assert made[0].calls[-1]["order_type"] == "MARKET"


def test_kis_class_share_round_trip_positions_open_orders_place_cancel_status(live_safety):
    client, api, scope = Mock(), Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=False)
    client.account.return_value = scope
    api.get_balances.return_value = {"json_data": {"rt_cd": "0", "output1": [{
        "pdno": "BRK/B", "prdt_name": "Class B", "ccld_qty_smtl1": "2", "ord_psbl_qty1": "2",
        "avg_unpr3": "100", "ovrs_now_pric1": "100", "frcr_evlu_amt2": "200"}]}}
    api.get_open_orders_overseas_all.return_value = {"json_data": {"rt_cd": "0", "output": [{
        "odno": "42", "pdno": "BRK/B", "nccs_qty": "1", "ord_gno_brno": "branch"}]}}
    api.post_order_overseas.return_value = {"json_data": {"rt_cd": "0", "output": {"ODNO": "42"}}}
    scope.overseas_cancel.return_value = {"json_data": {"rt_cd": "0", "output": {"ODNO": "43"}}}
    api.get_orders_overseas_all.return_value = {"json_data": {"rt_cd": "0", "output": [{
        "odno": "42", "pdno": "BRK/B", "ft_ord_qty": "1", "ft_ccld_qty": "1", "nccs_qty": "0",
        "rvse_cncl_dvsn": "00", "ft_ccld_unpr3": "100"}]}}
    live_safety.valuation = lambda request: Decimal("1000")
    broker = KIS(account="fixture", market="US", venue="NYSE", client=client, api=api, safety=live_safety)
    assert broker.get_positions()[0].symbol == "BRK.B"
    assert broker.available_qty("BRK.B") == 2
    assert broker.list_orders("BRK.B")[0].symbol == "BRK.B"
    order = broker.place_order(OrderRequest(symbol="BRK.B", market="US", venue="NYSE", side="buy", quantity=1, price="100", client_key="place-key"))
    assert (order.symbol, order.client_key) == ("BRK.B", "place-key")
    assert api.post_order_overseas.call_args.kwargs["ticker"] == "BRK/B"
    status = broker.status(order)
    assert (status.symbol, status.status, status.client_key) == ("BRK.B", "filled", "place-key")
    cancelled = broker.cancel_order(CancelRequest(symbol="BRK.B", market="US", venue="NYSE", order_id="42", quantity=1, client_key="cancel-key"))
    assert (cancelled.symbol, cancelled.client_key) == ("BRK.B", "cancel-key")
    assert scope.overseas_cancel.call_args.kwargs["code"] == "BRK/B"


@pytest.mark.parametrize("adapter", ["kiwoom", "kis", "dbfi"])
def test_close_without_session_authorization_never_transmits(adapter, kis, dbfi):
    from pykbroker.exceptions import RiskCapError

    world = World()
    choices = {"kiwoom": world.broker(), "kis": kis[0], "dbfi": dbfi[0]}
    broker = choices[adapter]
    broker.safety.valuation = lambda request: Decimal("70000")
    before = len(world.server.calls)
    for mock in (*kis[1:], *dbfi[1:]):
        mock.reset_mock()
    request = OrderRequest(symbol="005930", side="buy", quantity=1, order_type="close", client_key="close-key")
    with pytest.raises(RiskCapError, match="session authorization"):
        broker.place_order(request)
    assert len(world.server.calls) == before
    assert all(not mock.mock_calls for mock in (*kis[1:], *dbfi[1:]))


def test_common_status_returns_original_client_key(dbfi):
    broker, client, _ = dbfi
    order = broker.place_order(OrderRequest(symbol="005930", side="buy", quantity=1,
                                            price="70000", client_key="status-key"))
    client.get_transaction_history.return_value[0]["Out1"][0]["OrdNo"] = order.id
    status = broker.status(order)
    assert (status.id, status.status, status.client_key) == (order.id, "partial", "status-key")
    client.get_transaction_history.return_value = []
    missing = broker.status(order)
    assert (missing.status, missing.client_key) == ("unknown", "status-key")


def test_kiwoom_place_key_cannot_send_an_amendment():
    world = World()
    broker = world.broker()
    order = broker.place_order(buy("shared-key", qty=1))
    before = len(world.server.calls)
    with pytest.raises(DuplicateOrderError):
        broker.amend_order(AmendRequest(**buy("shared-key", qty=1).model_dump(), order_id=order.id))
    assert len(world.server.calls) == before
    assert len(world.server.orders) == 1


def test_kiwoom_common_amend_shape_echoes_key_and_reserves_place_namespace():
    world = World()
    broker = world.broker()
    order = broker.place_order(buy("original-key", qty=1))
    changed = broker.amend_order(buy("amend-key", qty=1), order_id=order.id, broker_ref={})
    assert (changed.status, changed.client_key) == ("accepted", "amend-key")
    before = len(world.server.calls)
    with pytest.raises(DuplicateOrderError):
        broker.place_order(buy("amend-key", qty=1))
    assert len(world.server.calls) == before


def test_kiwoom_single_price_market_is_refused_before_any_http():
    world = World()
    broker = world.broker()
    broker.safety.market_order_allowed = lambda request: True
    broker.safety.valuation = lambda request: Decimal("70000")
    before = len(world.server.calls)
    request = OrderRequest(symbol="005930", side="buy", quantity=1, order_type="market",
                           session="single_price", client_key="single-market")
    with pytest.raises(NotSupportedError):
        broker.place_order(request)
    assert len(world.server.calls) == before
