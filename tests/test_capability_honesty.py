"""A′-4: capabilities() is what the executor plans against, so it must match real behaviour."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pykbroker.brokers.dbfi import DBFI
from pykbroker.brokers.kis import KIS
from pykbroker.exceptions import ConfigurationError, NotSupportedError
from pykbroker.models import OrderRequest

KR = [(KIS, {}), (DBFI, {})]
US = [(KIS, {"venue": "NASDAQ"}), (DBFI, {"venue": "NASDAQ"})]


def _broker(broker_type, market, live_safety, **kwargs):
    client, api = Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=False)
    return broker_type(account="fixture", market=market, client=client, api=api, safety=live_safety, **kwargs)


def _market_order(market):
    return OrderRequest(
        symbol="005930" if market == "KR" else "AAPL",
        side="buy",
        quantity=1,
        order_type="market",
        market=market,
        client_key="cap",
    )


@pytest.mark.parametrize("broker_type,kwargs", US)
def test_us_admits_it_has_no_market_order(broker_type, kwargs, live_safety):
    caps = _broker(broker_type, "US", live_safety, **kwargs).capabilities()
    assert caps.supports_market_order is False
    assert caps.supports_limit_order is True
    assert caps.markets == ("US",)


@pytest.mark.parametrize("broker_type,kwargs", US)
def test_us_declares_that_a_venue_is_required_and_which_ones(broker_type, kwargs, live_safety):
    caps = _broker(broker_type, "US", live_safety, **kwargs).capabilities()
    assert caps.requires_venue is True
    assert set(caps.venues) == {"NASDAQ", "NYSE", "AMEX"}
    assert kwargs["venue"] in caps.venues


@pytest.mark.parametrize("broker_type,kwargs", KR)
def test_kr_declares_its_own_venues_and_keeps_market_orders(broker_type, kwargs, live_safety):
    caps = _broker(broker_type, "KR", live_safety, **kwargs).capabilities()
    assert caps.supports_market_order is True
    assert caps.requires_venue is False
    assert "KRX" in caps.venues
    assert ("NXT" in caps.venues) is caps.supports_nxt


@pytest.mark.parametrize("broker_type,market,kwargs", [(t, "KR", k) for t, k in KR] + [(t, "US", k) for t, k in US])
def test_the_market_order_claim_matches_what_actually_happens(broker_type, market, kwargs, live_safety):
    """Bypass 1: a capability that disagrees with behaviour is a lie the executor would act on."""
    broker = _broker(broker_type, market, live_safety, **kwargs)
    if broker.capabilities().supports_market_order:
        return
    with pytest.raises(NotSupportedError):
        broker.place_order(_market_order(market))


@pytest.mark.parametrize("broker_type,kwargs", US)
def test_a_constructed_request_cannot_smuggle_a_us_market_order(broker_type, kwargs, live_safety):
    """Bypass 2: model_construct skips validation, so place_order revalidates and still refuses."""
    broker = _broker(broker_type, "US", live_safety, **kwargs)
    smuggled = OrderRequest.model_construct(
        symbol="AAPL",
        side="buy",
        quantity=1,
        order_type="market",
        price=None,
        market="US",
        venue=None,
        client_key="cap",
    )
    with pytest.raises(NotSupportedError):
        broker.place_order(smuggled)


@pytest.mark.parametrize("broker_type,kwargs", US)
def test_a_venue_outside_the_declared_list_is_refused(broker_type, kwargs, live_safety):
    """Bypass 3: the venue list is exhaustive, not decorative."""
    caps = _broker(broker_type, "US", live_safety, **kwargs).capabilities()
    for bad in ("", "LSE", "KRX", "nasdaq"):
        assert bad not in caps.venues
        with pytest.raises(ConfigurationError):
            _broker(broker_type, "US", live_safety, venue=bad)


@pytest.mark.parametrize("broker_type,kwargs", US)
def test_a_us_order_routed_to_another_declared_venue_is_refused(broker_type, kwargs, live_safety):
    broker = _broker(broker_type, "US", live_safety, **kwargs)
    order = OrderRequest(
        symbol="AAPL", side="buy", quantity=1, price="120", market="US", venue="NYSE", client_key="cap"
    )
    with pytest.raises(NotSupportedError):
        broker.place_order(order)


# --- B′2-3: 정직성 검사를 KR/US 매트릭스와 취소까지 --------------------------

from unittest.mock import Mock as _Mock  # noqa: E402

from pykbroker.brokers.toss import Toss  # noqa: E402
from pykbroker.exceptions import SpecUnverifiedError  # noqa: E402
from pykbroker.models import CancelRequest  # noqa: E402

SHIPPED = [KIS, DBFI, Toss]


def _any_broker(broker_type, market, live_safety, **kwargs):
    if broker_type is Toss:
        return Toss(account="fixture", market=market, account_seq=1, sdk_factory=_Mock(), safety=live_safety, **kwargs)
    return _broker(broker_type, market, live_safety, **kwargs)


@pytest.mark.parametrize("broker_type", SHIPPED)
@pytest.mark.parametrize("market", ["KR", "US"])
def test_a_market_is_either_declared_with_venues_or_locked(broker_type, market, live_safety):
    kwargs = {"venue": "NASDAQ"} if market == "US" and broker_type is not Toss else {}
    try:
        caps = _any_broker(broker_type, market, live_safety, **kwargs).capabilities()
    except SpecUnverifiedError:
        return
    assert caps.markets == (market,)
    assert caps.venues


@pytest.mark.parametrize("broker_type", SHIPPED)
@pytest.mark.parametrize("market", ["KR", "US"])
def test_the_single_cancel_claim_matches_what_actually_happens(broker_type, market, live_safety):
    """우회 1: 「단일 취소 지원」 선언과 실제 거절이 어긋나면 실행기가 그 거짓말대로 움직인다."""
    kwargs = {"venue": "NASDAQ"} if market == "US" and broker_type is not Toss else {}
    try:
        broker = _any_broker(broker_type, market, live_safety, **kwargs)
    except SpecUnverifiedError:
        return
    caps = broker.capabilities()
    request = CancelRequest(
        order_id="42",
        symbol="005930" if market == "KR" else "AAPL",
        quantity=1,
        market=market,
        venue=kwargs.get("venue", "KRX"),
        client_key="honesty-cancel",
    )
    if caps.supports_single_order_cancel:
        broker._validate_order(request)
        return
    with pytest.raises(NotSupportedError):
        broker.cancel_order(request)


@pytest.mark.parametrize("broker_type", SHIPPED)
def test_a_locked_market_is_locked_before_any_capability_is_read(broker_type, live_safety):
    """우회 2: 잠긴 시장은 capabilities() 를 읽어 「부분 지원」으로 해석할 여지도 남기지 않는다."""
    try:
        broker = _any_broker(broker_type, "US", live_safety, **({} if broker_type is Toss else {"venue": "NASDAQ"}))
    except SpecUnverifiedError:
        return
    assert broker.capabilities().requires_venue is True
