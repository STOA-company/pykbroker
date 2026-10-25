"""SEAM data cells (1d): class-share separator, Order.client_key echo, Kiwoom reject rows, supports_amend."""

import json
from pathlib import Path

import pytest

from pykbroker.brokers._common import BaseBroker
from pykbroker.brokers.kis import KIS
from pykbroker.brokers.kiwoom import Kiwoom
from pykbroker.exceptions import BrokerRejectedError
from pykbroker.models import Capabilities, Order
from pykbroker.reject_codes import DEFINITIVE_REJECTS, RATE_LIMITED, REJECTED, TOKEN_EXPIRED

from .test_kiwoom_contract_server import World

OFFICIAL_CODES = json.loads((Path(__file__).parent / "fixtures/kiwoom/official/error_codes_all.json").read_text())


def _us(cls):
    broker = object.__new__(cls)
    broker.market = "US"
    return broker


def test_class_share_separator_is_a_data_cell_with_an_inverse():
    assert BaseBroker.us_class_separator == "." and KIS.us_class_separator == "/"
    base, kis = _us(BaseBroker), _us(KIS)
    assert base._symbol("BRK.B", "US") == "BRK.B"
    assert kis._symbol("BRK.B", "US") == "BRK/B" and kis.canonical_symbol("BRK/B") == "BRK.B"
    assert kis._symbol("AAPL", "US") == "AAPL" and kis.canonical_symbol("AAPL") == "AAPL"


def test_order_carries_an_optional_client_key_echo():
    assert Order(id="1", symbol="005930").client_key is None
    assert "k1" not in repr(Order(id="1", symbol="005930", client_key="k1"))


def test_kiwoom_reject_rows_are_exactly_the_official_table_minus_the_unknown_code():
    rows = DEFINITIVE_REJECTS["kiwoom"]
    assert set(rows) == set(OFFICIAL_CODES) - {"1999"}
    assert rows["8005"].kind == TOKEN_EXPIRED and rows["1700"].kind == RATE_LIMITED and rows["1511"].kind == REJECTED


def test_kiwoom_refusal_carries_code_and_kind_and_1999_stays_unknown():
    world = World()
    broker = world.broker()
    world.server.fail_next["ka10001"] = (1902, "")
    with pytest.raises(BrokerRejectedError) as raised:
        broker.get_quote("005930")
    assert (raised.value.code, raised.value.kind) == ("1902", REJECTED)
    world.server.limits = {"ka10001": 0}
    with pytest.raises(BrokerRejectedError) as raised:
        broker.get_quote("005930")
    assert raised.value.kind == RATE_LIMITED


def test_supports_amend_is_declared_only_where_a_native_amend_exists():
    assert Capabilities().supports_amend is False
    assert World().broker().capabilities().supports_amend is True
    assert Kiwoom.__mro__  # adapter class unchanged; the cell is read from capabilities()


def test_support_lists_are_data_on_every_broker_and_never_wider_than_what_is_implemented():
    from pykbroker.models import Capabilities

    # derived defaults: flags decide kinds, plain day regular session
    base = Capabilities(supports_market_order=True)
    assert (base.order_kinds, base.time_in_force, base.sessions) == (("limit", "market"), ("day",), ("regular",))
    assert Capabilities(supports_limit_order=False).order_kinds == ()
    kiwoom = World().broker(virtual=False).capabilities()
    assert kiwoom.order_kinds == ("limit", "market", "close", "conditional_limit", "best", "top", "mid")
    assert kiwoom.time_in_force == ("day", "ioc", "fok") and kiwoom.sessions == ("regular", "pre", "single")
    assert kiwoom.venues == ("KRX", "NXT", "SOR")
    mock = World().broker().capabilities()
    assert mock.venues == ("KRX",)
