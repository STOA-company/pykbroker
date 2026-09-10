"""B′2-1: 「거래소를 반드시 대라」와 「댈 거래소가 없다」는 동시에 참일 수 없다.

A′ 2라운드가 `requires_venue` 를 강제로 올린 뒤, base 기본값은 US 에서
`requires_venue=True` 와 `venues=()` 를 함께 내놓았다. 이 모순쌍을 상속한 브로커는
venue 를 대도 「모르는 거래소」, 안 대도 「거래소를 대라」 — 주문도 취소도 전부 거절이다.

모순은 «쓰이는 순간»이 아니라 «만들어지는 순간»에 막아야 한다. 그래서 검증은
`Capabilities` 자신이 지고, capabilities() 는 `model_copy(update=)`(검증을 건너뛴다)가
아니라 값을 다시 만들어 그 검증을 반드시 통과시킨다.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from pykbroker.brokers._common import BaseBroker
from pykbroker.brokers.dbfi import DBFI
from pykbroker.brokers.kis import KIS
from pykbroker.brokers.toss import Toss
from pykbroker.exceptions import NotSupportedError
from pykbroker.models import Capabilities, OrderRequest

SHIPPED = [KIS, DBFI, Toss]


def _built(broker_type, **kwargs):
    client, api = Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=False)
    if broker_type is Toss:
        return Toss(account="fixture", account_seq=1, sdk_factory=Mock(), **kwargs)
    return broker_type(account="fixture", client=client, api=api, **kwargs)


def test_a_contradictory_capability_cannot_be_constructed():
    with pytest.raises(ValidationError, match="venue"):
        Capabilities(requires_venue=True, venues=())


def test_a_venue_requirement_with_a_declared_list_is_fine():
    caps = Capabilities(requires_venue=True, venues=("NASDAQ",))
    assert caps.venues == ("NASDAQ",)


def test_a_broker_may_still_leave_venues_empty_when_it_requires_none():
    assert Capabilities(requires_venue=False, venues=()).venues == ()


def test_model_copy_skips_validation_so_capabilities_must_rebuild():
    """왜 `model_copy(update=)` 로는 못 막나 — 그 경로는 검증을 아예 타지 않는다."""
    caps = Capabilities(requires_venue=False, venues=("KRX",))
    assert caps.model_copy(update={"requires_venue": True, "venues": ()}).venues == ()
    with pytest.raises(ValidationError, match="venue"):
        Capabilities(**{**caps.model_dump(), "requires_venue": True, "venues": ()})


def test_a_broker_that_drops_its_venues_fails_at_construction_not_at_order_time():
    class _Sloppy(BaseBroker):
        broker_code = "sloppy"

        def capabilities(self) -> Capabilities:
            base = super().capabilities()
            return Capabilities(**{**base.model_dump(), "requires_venue": True, "venues": ()})

    with pytest.raises(ValidationError, match="venue"):
        _Sloppy(account="fixture").capabilities()


@pytest.mark.parametrize("broker_type", SHIPPED)
def test_every_shipped_broker_survives_its_own_validation(broker_type):
    caps = _built(broker_type).capabilities()
    assert not (caps.requires_venue and not caps.venues)


def test_the_base_default_no_longer_ships_the_contradictory_pair():
    """근본원인: base 가 US 에서 requires_venue=True·venues=() 를 내놓던 자리."""
    broker = BaseBroker(account="fixture", market="US")
    with pytest.raises(NotSupportedError, match="venue"):
        broker.capabilities()


# --- 우회 ---------------------------------------------------------------------


@pytest.mark.parametrize("broker_type", SHIPPED)
def test_a_construct_smuggled_contradiction_still_refuses_the_order(broker_type, live_safety, monkeypatch):
    """우회 1: `model_construct` 는 검증을 건너뛴다 — 그래도 주문은 fail-closed 여야 한다."""
    broker = _built(broker_type, safety=live_safety)
    smuggled = Capabilities.model_construct(
        **{**broker.capabilities().model_dump(), "requires_venue": True, "venues": ()}
    )
    monkeypatch.setattr(type(broker), "capabilities", lambda self: smuggled)
    order = OrderRequest(symbol="005930", side="buy", quantity=1, price="70000", client_key="bypass-1")
    with pytest.raises(NotSupportedError):
        broker.place_order(order)
    with pytest.raises(NotSupportedError):
        broker.place_order(order.model_copy(update={"venue": "KRX", "client_key": "bypass-1b"}))


def test_venues_cannot_be_emptied_after_construction():
    """우회 2: 만들어 놓고 나중에 비우는 길 — 모델이 frozen 이라 닫혀 있다."""
    caps = Capabilities(requires_venue=True, venues=("NASDAQ",))
    with pytest.raises(ValidationError):
        caps.venues = ()
    assert caps.venues == ("NASDAQ",)


def test_toss_rebuilds_its_capabilities_through_the_validation(monkeypatch):
    """확장 경로가 검증을 타는지 — base 가 모순쌍을 내놓으면 확장도 거기서 멈춰야 한다."""
    broker = _built(Toss)
    contradictory = Capabilities.model_construct(
        **{**Capabilities().model_dump(), "requires_venue": True, "venues": ()}
    )
    monkeypatch.setattr(BaseBroker, "capabilities", lambda self: contradictory)
    with pytest.raises(ValidationError, match="venue"):
        broker.capabilities()
