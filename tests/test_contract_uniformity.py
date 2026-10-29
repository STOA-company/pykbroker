"""A′-5: DB Securities satisfies the same contract as KIS, and the experimental lock still holds."""

import inspect
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import pykbroker
from pykbroker.base import BrokerAdapter
from pykbroker.brokers.dbfi import DBFI
from pykbroker.brokers.kb import KB, KB_API_BASE_URL
from pykbroker.brokers.kis import KIS
from pykbroker.brokers.kiwoom import Kiwoom
from pykbroker.brokers.ls import LS, LS_API_BASE_URL
from pykbroker.brokers.nh import NH
from pykbroker.brokers.toss import Toss
from pykbroker.exceptions import ConfigurationError, NotSupportedError, SpecUnverifiedError
from pykbroker.factory import BROKERS, create_broker
from pykbroker.models import CancelRequest, OrderRequest


def _locked_client(module_name):
    return {"client": SimpleNamespace(base_url="https://openapi.ls-sec.co.kr:8080")} if module_name == "ls" else {}


SHIPPED = [KIS, DBFI, Toss, NH, Kiwoom, LS, KB]
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
    if broker_type is NH:
        return NH(
            account="fixture",
            client_options={"app_key": "fixture-key", "token": "fixture", "acct_type": "01"},
            client_factory=lambda **options: SimpleNamespace(**options),
            allow_live_orders=True,
            **kwargs,
        )
    if broker_type is Toss:
        return Toss(account="fixture", account_seq=1, sdk_factory=Mock(), **kwargs)
    if broker_type is Kiwoom:
        return Kiwoom(
            account="fixture",
            client_options={"app_key": "fixture-key", "token": "fixture-token"},
            client_factory=lambda **options: SimpleNamespace(**options),
            **kwargs,
        )
    if broker_type is LS:
        client.base_url = LS_API_BASE_URL
        return LS(
            account="fixture", client=client,
            client_options={"token": "fixture-token"}, allow_live_orders=True, **kwargs,
        )
    if broker_type is KB:
        client.base_url = KB_API_BASE_URL
        return KB(
            account="fixture", bound_account="fixture", client=client,
            app_key="fixture-key", token="fixture-token", writes_enabled=True, **kwargs,
        )
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
    # LS/KB forward these public arguments through **kwargs to BaseBroker.
    _built(broker_type, **{argument: False if argument == "virtual" else lambda symbol: False})


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
    broker = getattr(module, class_name)(account="fixture", safety=live_safety, **_locked_client(module_name))
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

    LS accepts an explicit mode but refuses an unverified mock address. Other locked
    mappings reject unsupported modes. None claims a verified mock-account contract.
    """
    module = import_module("pykbroker.experimental." + module_name)
    broker_type = getattr(module, class_name)
    assert (
        broker_type(account="fixture", **_locked_client(module_name)).capabilities().supports_virtual_account is False
    )
    with pytest.raises((NotSupportedError, TypeError, ConfigurationError)):
        broker_type(account="fixture", virtual=True, safety=live_safety, **_locked_client(module_name))
    with pytest.raises(SpecUnverifiedError):
        broker_type(account="fixture", safety=live_safety, **_locked_client(module_name)).place_order(request_order)


@pytest.mark.parametrize("module_name,class_name", LOCKED)
@pytest.mark.parametrize("case", [str.lower, str.upper, str.title])
def test_experimental_brokers_are_not_reachable_through_the_factory(module_name, class_name, case):
    """Registered REST adapters require credentials; the LS compatibility alias stays locked."""
    experimental = getattr(import_module("pykbroker.experimental." + module_name), class_name)
    if module_name == "ls":
        assert BROKERS["ls"] is experimental
        broker = create_broker(case(module_name), account="fixture", **_locked_client(module_name))
        with pytest.raises(SpecUnverifiedError):
            broker.place_order(_kr_order())
        with pytest.raises(ConfigurationError):
            create_broker(case(module_name), account="fixture")
        return
    assert BROKERS.get(module_name) is not experimental and class_name not in BROKERS
    assert class_name not in pykbroker.__all__
    with pytest.raises(ConfigurationError):
        create_broker(case(module_name), account="fixture")


def test_the_factory_ships_exactly_the_registered_adapters():
    assert set(BROKERS) == {"kis", "dbfi", "toss", "kiwoom", "ls", "nh", "kb", "saxo"}
    from pykbroker.brokers.saxo import Saxo  # S2: 계약 균일성 SHIPPED 매개변수화 밖(자체 시험 보유)
    assert set(BROKERS.values()) == set(SHIPPED) | {Saxo}


# --- B′-0: 거래소 선언 정합 --------------------------------------------------
#
# A′ 2라운드는 `requires_venue` 를 «선언만»에서 «강제»로 올렸다(tests/test_venue_required.py).
# 그 순간, capabilities() 를 손으로 다시 적으면서 venues 를 빠뜨린 브로커는 venue 를 채운
# 주문이 **전부** NotSupportedError 가 된다 — Toss 가 정확히 그랬다(국내 주문 전멸).
# 선언과 검증은 어긋나면 안 된다: 선언한 거래소는 반드시 통과하고, 선언하지 않은 이름은
# 반드시 막힌다. 정합을 재는 쪽은 브로커별 하드코딩이 아니라 capabilities() 자신이다.

KR_DEFAULT_VENUE = "KRX"


def _kr_order(**kwargs):
    return OrderRequest(symbol="005930", side="buy", quantity=1, price="70000", client_key="venue-uniformity", **kwargs)


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


# --- B′2-3: 거래소 정합을 KR/US 매트릭스와 «취소»까지 넓힌다 -------------------
#
# 1라운드는 국내 4종만 쟀다. 회귀가 태어난 자리(US 기본값의 모순쌍)와, 회귀가 실제로
# 죽였던 두 번째 경로(취소)는 한 번도 재본 적이 없다. 절반만 잼 = 절반은 무방비다.

MATRIX = [
    (KIS, "KR", {}),
    (DBFI, "KR", {}),
    (Toss, "KR", {}),
    (KIS, "US", {"venue": "NASDAQ"}),
    (DBFI, "US", {"venue": "NASDAQ"}),
]
US_MATRIX = [row for row in MATRIX if row[1] == "US"]


def _for_market(broker_type, market, **kwargs):
    return _built(broker_type, market=market, **kwargs)


def _request(kind, market, **kwargs):
    symbol = "005930" if market == "KR" else "AAPL"
    if kind == "order":
        return OrderRequest(
            symbol=symbol, side="buy", quantity=1, price="70000", market=market, client_key="matrix", **kwargs
        )
    return CancelRequest(order_id="42", symbol=symbol, quantity=1, market=market, client_key="matrix", **kwargs)


@pytest.mark.parametrize("broker_type,market,kwargs", MATRIX)
def test_every_market_declares_the_venues_it_can_actually_route(broker_type, market, kwargs, live_safety):
    caps = _for_market(broker_type, market, safety=live_safety, **kwargs).capabilities()
    assert caps.markets == (market,)
    assert caps.venues, "선언이 비면 venue 를 채운 주문·취소가 전부 거절된다"
    assert caps.requires_venue is (market == "US")
    if caps.requires_venue:
        assert kwargs["venue"] in caps.venues


@pytest.mark.parametrize("broker_type,market,kwargs", MATRIX)
@pytest.mark.parametrize("kind", ["order", "cancel"])
def test_the_configured_venue_passes_the_validation_for_orders_and_cancels(
    broker_type, market, kwargs, kind, live_safety
):
    broker = _for_market(broker_type, market, safety=live_safety, **kwargs)
    caps = broker.capabilities()
    venue = kwargs.get("venue", KR_DEFAULT_VENUE)
    request = _request(kind, market, venue=venue)
    if kind == "cancel" and not caps.supports_single_order_cancel:
        with pytest.raises(NotSupportedError):
            broker._validate_order(request)
        return
    broker._validate_order(request)


@pytest.mark.parametrize("broker_type,market,kwargs", US_MATRIX)
@pytest.mark.parametrize("kind", ["order", "cancel"])
def test_a_us_request_that_names_no_venue_is_refused_including_cancels(broker_type, market, kwargs, kind, live_safety):
    broker = _for_market(broker_type, market, safety=live_safety, **kwargs)
    with pytest.raises(NotSupportedError, match="venue"):
        broker._validate_order(_request(kind, market))


@pytest.mark.parametrize("broker_type,market,kwargs", MATRIX)
@pytest.mark.parametrize("kind", ["order", "cancel"])
@pytest.mark.parametrize("bogus", ["LSE", "nasdaq", "KRX " if True else "", "TSE"])
def test_an_undeclared_exchange_is_refused_on_both_paths(broker_type, market, kwargs, kind, bogus, live_safety):
    """우회 1: 취소는 「이미 낸 주문을 거두는 것」이라며 거래소 검사를 건너뛰지 않는다."""
    broker = _for_market(broker_type, market, safety=live_safety, **kwargs)
    assert bogus not in broker.capabilities().venues
    with pytest.raises(NotSupportedError):
        broker._validate_order(_request(kind, market, venue=bogus))


@pytest.mark.parametrize("broker_type,market,kwargs", US_MATRIX)
def test_a_constructed_cancel_cannot_smuggle_a_missing_venue(broker_type, market, kwargs, live_safety):
    """우회 2: model_construct 로 검증을 건너뛴 취소도 cancel_order 가 다시 검증한다."""
    broker = _for_market(broker_type, market, safety=live_safety, **kwargs)
    smuggled = CancelRequest.model_construct(
        order_id="42", symbol="AAPL", quantity=1, market="US", venue=None, client_key="matrix"
    )
    with pytest.raises(NotSupportedError):
        broker.cancel_order(smuggled)


@pytest.mark.parametrize("broker_type,market,kwargs", US_MATRIX)
def test_another_declared_us_venue_is_still_refused_on_cancels(broker_type, market, kwargs, live_safety):
    """우회 3: 선언 목록 안이라도 이 인스턴스가 설정한 거래소가 아니면 취소도 나가지 않는다."""
    broker = _for_market(broker_type, market, safety=live_safety, **kwargs)
    other = next(v for v in broker.capabilities().venues if v != kwargs["venue"])
    with pytest.raises(NotSupportedError):
        broker._validate_order(_request("cancel", market, venue=other))


@pytest.mark.parametrize("broker_type", SHIPPED)
@pytest.mark.parametrize("market", ["KR", "US"])
def test_a_broker_either_routes_a_market_or_locks_it(broker_type, market, live_safety):
    """B′2-3 의 본체: 시장마다 「댈 거래소가 있다」거나 「잠겼다」거나, 둘 중 하나여야 한다.

    회귀는 정확히 그 사이에서 태어났다 — 선언은 US 를 지원한다고 했고, 댈 거래소는
    비어 있었고, 그래서 주문도 취소도 전부 거절됐다. 그 상태를 여기서 못박는다.
    """
    takes_venue = "venue" in inspect.signature(broker_type.__init__).parameters
    kwargs = {"venue": "NASDAQ"} if market == "US" and takes_venue else {}
    if broker_type is NH and market == "US":
        with pytest.raises(ConfigurationError, match="US for US"):
            _for_market(broker_type, market, safety=live_safety, **kwargs)
        kwargs = {"venue": "US"}
    try:
        caps = _for_market(broker_type, market, safety=live_safety, **kwargs).capabilities()
    except SpecUnverifiedError:
        pytest.skip(f"{broker_type.__name__}/{market} 은 스펙 미검증으로 잠겨 있다 — 이 매트릭스 검사 대상이 아니다")
    assert caps.markets == (market,)
    assert caps.venues, "지원한다고 선언한 시장에 댈 거래소가 없다 — 주문·취소가 전부 거절된다"
