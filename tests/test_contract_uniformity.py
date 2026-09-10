"""A′-5: DB Securities satisfies the same contract as KIS, and the experimental lock still holds."""

import inspect
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import pykbroker
from pykbroker.base import BrokerAdapter
from pykbroker.brokers.dbfi import DBFI
from pykbroker.brokers.kis import KIS
from pykbroker.brokers.toss import Toss
from pykbroker.exceptions import ConfigurationError, NotSupportedError, SpecUnverifiedError
from pykbroker.factory import BROKERS, create_broker
from pykbroker.models import CancelRequest, OrderRequest

SHIPPED = [KIS, DBFI, Toss]
LOCKED = [("ls", "LS"), ("kiwoom", "Kiwoom"), ("nh", "NH"), ("kb", "KB")]
# Every question the settlement-aware executor asks a broker, whatever the asset class.
CONTRACT = (
    "quote balance positions rules place cancel status "
    "get_quote get_balance get_positions get_rules list_orders capabilities "
    "available_cash available_qty close"
).split()


def _built(broker_type, **kwargs):
    client, api = Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=False)
    if broker_type is Toss:
        return Toss(account="fixture", account_seq=1, sdk_factory=Mock(), **kwargs)
    return broker_type(account="fixture", client=client, api=api, **kwargs)


@pytest.mark.parametrize("broker_type", SHIPPED)
def test_every_shipped_broker_answers_the_whole_contract(broker_type):
    broker = _built(broker_type)
    assert isinstance(broker, BrokerAdapter)
    for name in CONTRACT:
        assert callable(getattr(broker, name)), name


@pytest.mark.parametrize("broker_type", SHIPPED)
@pytest.mark.parametrize("argument", ["virtual", "halt_provider"])
def test_the_account_mode_and_halt_source_are_uniform_arguments(broker_type, argument):
    assert argument in inspect.signature(broker_type.__init__).parameters


@pytest.mark.parametrize("broker_type", SHIPPED)
def test_get_rules_takes_the_clock_and_the_calendar_everywhere(broker_type):
    parameters = inspect.signature(broker_type.get_rules).parameters
    assert {"now", "trading_day"} <= set(parameters)


def test_dbfi_rules_and_funds_match_the_kis_shapes(dbfi, kis):
    db_rules, kis_rules = dbfi[0].get_rules("005930"), kis[0].get_rules("005930")
    assert db_rules.model_fields_set <= set(type(kis_rules).model_fields)
    assert (db_rules.lot, db_rules.market, db_rules.currency) == (kis_rules.lot, "KR", "KRW")
    assert dbfi[0].available_cash() == kis[0].available_cash()


@pytest.mark.parametrize("broker_type", [DBFI, Toss])
def test_a_broker_without_a_mock_domain_says_so_and_refuses_one(broker_type):
    assert _built(broker_type).capabilities().supports_virtual_account is False
    with pytest.raises(NotSupportedError):
        _built(broker_type, virtual=True)


def test_only_kis_claims_a_mock_domain():
    assert _built(KIS).capabilities().supports_virtual_account is True


# --- the experimental lock ------------------------------------------------


@pytest.mark.parametrize("module_name,class_name", LOCKED)
@pytest.mark.parametrize("operation", ["place_order", "place", "cancel_order", "cancel"])
def test_locked_brokers_still_refuse_every_mutation(module_name, class_name, operation, request_order, live_safety):
    module = import_module("pykbroker.experimental." + module_name)
    broker = getattr(module, class_name)(account="fixture", safety=live_safety)
    request = (
        request_order
        if operation.startswith("place")
        else CancelRequest(order_id="42", symbol="005930", quantity=1, client_key="lock")
    )
    with pytest.raises(SpecUnverifiedError):
        getattr(broker, operation)(request)


@pytest.mark.parametrize("module_name,class_name", LOCKED)
def test_a_mock_flag_does_not_unlock_an_experimental_broker(module_name, class_name, request_order, live_safety):
    """Bypass 1: the new mock-account argument must not become a side door.

    The two REST-backed mappings refuse it hardest of all — A′ left their constructor
    alone, so the argument does not exist there. The two skeletons forward **kwargs and
    are refused by the base contract instead. Either way, no mock domain is claimed.
    """
    module = import_module("pykbroker.experimental." + module_name)
    broker_type = getattr(module, class_name)
    assert broker_type(account="fixture").capabilities().supports_virtual_account is False
    with pytest.raises((NotSupportedError, TypeError)):
        broker_type(account="fixture", virtual=True, safety=live_safety)
    with pytest.raises(SpecUnverifiedError):
        broker_type(account="fixture", safety=live_safety).place_order(request_order)


@pytest.mark.parametrize("module_name,class_name", LOCKED)
@pytest.mark.parametrize("case", [str.lower, str.upper, str.title])
def test_locked_brokers_are_not_reachable_through_the_factory(module_name, class_name, case):
    """Bypass 2: the public entry point ships three brokers, and case folding adds none."""
    assert module_name not in BROKERS and class_name not in BROKERS
    assert class_name not in pykbroker.__all__
    with pytest.raises(ConfigurationError):
        create_broker(case(module_name), account="fixture")


def test_the_factory_ships_exactly_the_verified_three():
    assert set(BROKERS) == {"kis", "dbfi", "toss"}


# --- B′-0: 거래소 선언 정합 --------------------------------------------------
#
# A′ 2라운드는 `requires_venue` 를 «선언만»에서 «강제»로 올렸다(tests/test_venue_required.py).
# 그 순간, capabilities() 를 손으로 다시 적으면서 venues 를 빠뜨린 브로커는 venue 를 채운
# 주문이 **전부** NotSupportedError 가 된다 — Toss 가 정확히 그랬다(국내 주문 전멸).
# 선언과 검증은 어긋나면 안 된다: 선언한 거래소는 반드시 통과하고, 선언하지 않은 이름은
# 반드시 막힌다. 정합을 재는 쪽은 브로커별 하드코딩이 아니라 capabilities() 자신이다.

KR_DEFAULT_VENUE = "KRX"


def _kr_order(**kwargs):
    return OrderRequest(
        symbol="005930", side="buy", quantity=1, price="70000", client_key="venue-uniformity", **kwargs
    )


@pytest.mark.parametrize("broker_type", SHIPPED)
def test_a_kr_broker_declares_the_exchange_its_orders_name(broker_type, live_safety):
    caps = _built(broker_type, safety=live_safety).capabilities()
    assert caps.markets == ("KR",)
    assert KR_DEFAULT_VENUE in caps.venues


@pytest.mark.parametrize("broker_type", SHIPPED)
def test_every_declared_venue_passes_the_validation_that_reads_it(broker_type, live_safety):
    broker = _built(broker_type, safety=live_safety)
    caps = broker.capabilities()
    assert caps.venues, "거래소 선언이 비면 venue 를 채운 주문이 전부 거절된다"
    for venue in caps.venues:
        broker._validate_order(_kr_order(venue=venue))


@pytest.mark.parametrize("broker_type", SHIPPED)
def test_the_nxt_claim_and_the_nxt_declaration_agree(broker_type, live_safety):
    caps = _built(broker_type, safety=live_safety).capabilities()
    assert caps.supports_nxt == ("NXT" in caps.venues)


@pytest.mark.parametrize("broker_type", SHIPPED)
@pytest.mark.parametrize("bogus", ["KOSPI", "krx", "KRX ", " "])
def test_an_undeclared_exchange_is_still_refused(broker_type, bogus, live_safety):
    """우회 1: «국내니까 괜찮다»로 선언 밖의 이름을 통과시키지 않는다."""
    with pytest.raises(NotSupportedError):
        _built(broker_type, safety=live_safety)._validate_order(_kr_order(venue=bogus))


@pytest.mark.parametrize("broker_type", [KIS, Toss])
def test_nxt_is_refused_where_it_is_not_declared(broker_type, live_safety):
    """우회 2: NXT 를 지원한다고 선언하지 않은 브로커에 이름으로 밀어 넣어도 나가지 않는다."""
    with pytest.raises(NotSupportedError):
        _built(broker_type, safety=live_safety)._validate_order(_kr_order(venue="NXT"))
