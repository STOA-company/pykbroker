"""The same canonical request reaches all four registered adapters; no broker-specific request shape."""
from pykbroker.models import OrderRequest

from .test_kb_contract_walk import world as kb_world  # noqa: F401
from .test_ls_contract_walk import world as ls_world  # noqa: F401
from .test_nh_contract_walk import world as nh_world  # noqa: F401
from .test_kiwoom_contract_server import World


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
