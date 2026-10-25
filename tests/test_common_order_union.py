"""The same canonical request reaches all four registered adapters; no broker-specific request shape."""
from pykbroker.models import OrderRequest

from . import test_kb_contract_walk as kb_walk
from . import test_ls_contract_walk as ls_walk
from . import test_nh_contract_walk as nh_walk
from .test_kiwoom_contract_server import World

kb_world, ls_world, nh_world = kb_walk.world, ls_walk.world, nh_walk.world

def test_four_brokers_accept_the_same_order_request_and_echo_the_same_client_key(kb_world, ls_world, nh_world):
    request = OrderRequest(symbol="005930", side="buy", quantity=1, price="1000", venue="KRX",
                           time_in_force="day", session="regular", client_key="common-order-key")
    for broker in (World().broker(), kb_world[1], ls_world[1], nh_world[1]):
        caps = broker.capabilities()
        assert request.order_type in caps.order_kinds
        assert caps.supports_amend and caps.amend_time_in_force == ("day",)
        assert caps.amend_sessions == ("regular",)
        result = broker.place_order(request)
        assert (result.symbol, result.client_key, result.status) == ("005930", "common-order-key", "accepted")
