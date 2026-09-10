"""B′2-2: 「미국주식 됩니다」라고 적어 놓고 전부 거절하는 상태를 없앤다.

토스 해외주식은 이 저장소에서 한 번도 벤더 명세로 확인된 적이 없다. 거래소 코드도,
주문 유형도, 취소 계약도 모른다. 그런데 capabilities() 는 `markets=("US",)` 를 선언하고
문서는 「Toss KR / US」라고 적었다 — 그리고 실제로는 모든 주문·취소가 거절됐다.

선언과 동작이 다르면 그건 결함이 아니라 거짓말이다. 확인하지 못한 것은 확인하지
못했다고 말하고 «잠근다». 잠금은 주문 시점이 아니라 생성 시점에 건다.
"""

import re
from pathlib import Path
from unittest.mock import Mock

import pytest

from pykbroker.brokers.toss import Toss
from pykbroker.exceptions import NotSupportedError, SpecUnverifiedError
from pykbroker.factory import create_broker

DOCS = Path(__file__).resolve().parent.parent / "docs" / "llms-full.txt"


def _toss(**kwargs):
    return Toss(account="fixture", account_seq=1, sdk_factory=Mock(), **kwargs)


def test_toss_us_is_locked_at_construction():
    with pytest.raises(SpecUnverifiedError, match="verified"):
        _toss(market="US")


def test_toss_kr_is_untouched():
    caps = _toss().capabilities()
    assert caps.markets == ("KR",)
    assert caps.requires_venue is False
    assert "KRX" in caps.venues


def test_toss_never_declares_a_market_it_cannot_route():
    assert _toss().capabilities().markets == ("KR",)


def test_the_documentation_no_longer_claims_toss_us():
    text = DOCS.read_text()
    rows = [line for line in text.splitlines() if re.match(r"\|\s*Toss\s*\|", line)]
    assert rows, "문서의 브로커 표에서 Toss 행을 찾지 못했다"
    for row in rows:
        assert "US" not in row.split("|")[2], row


# --- 우회 ---------------------------------------------------------------------


@pytest.mark.parametrize("market", ["US", "us", "Us", " US", "US "])
def test_no_spelling_of_the_us_market_opens_toss(market):
    """우회 1: 대소문자·공백으로 시장 이름을 바꿔 넣어도 열리지 않는다."""
    with pytest.raises(NotSupportedError):
        _toss(market=market)


def test_flipping_the_market_after_construction_still_refuses():
    """우회 2: 생성 뒤에 속성을 갈아끼워도 capabilities() 가 잠금을 다시 건다."""
    broker = _toss()
    broker.market = "US"
    with pytest.raises(SpecUnverifiedError):
        broker.capabilities()


def test_the_factory_cannot_open_toss_us(monkeypatch):
    """우회 3: 공개 진입점(create_broker)도 같은 잠금을 지난다."""
    monkeypatch.setenv("PYKBROKER_ACCOUNT", "fixture")
    monkeypatch.setenv("PYKBROKER_TOSS_ACCOUNT_SEQ", "1")
    with pytest.raises(NotSupportedError):
        create_broker("toss", account="fixture", account_seq=1, sdk_factory=Mock(), market="US")


def test_a_close_order_is_refused_because_the_overseas_session_is_locked():
    """종가(CLS) 주문은 잠긴 해외 세션에서만 나갈 수 있다 — 국내로 우회 접수되지 않는다.

    안전 게이트(캡·드라이런)보다 «안쪽»에서 막히는지를 본다: 번역 계층이 벤더로
    보내려 드는 순간이 여기다.
    """
    from pykbroker.models import OrderRequest

    broker = _toss()
    order = OrderRequest(
        symbol="005930", side="sell", quantity=1, price="70000", order_type="close", client_key="close-lock"
    )
    with pytest.raises(NotSupportedError):
        broker._place_order(order)
