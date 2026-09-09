from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pykbroker.rules.kr import normalize_symbol, regular_session_open, round_price, tick_size


@pytest.mark.parametrize(
    "price,tick",
    [(1999, 1), (2000, 5), (4999, 5), (5000, 10), (20000, 50), (50000, 100), (200000, 500), (500000, 1000)],
)
def test_tick_boundaries(price, tick):
    assert tick_size(price) == Decimal(tick)


def test_rounding_and_symbols():
    assert round_price("70099", "buy") == Decimal(70000)
    assert round_price("70001", "sell") == Decimal(70100)
    assert normalize_symbol("A005930") == "005930"
    assert normalize_symbol("005930") == "005930"


def test_session_needs_calendar_and_timezone():
    now = datetime(2026, 9, 9, 0, 0, tzinfo=UTC)
    assert not regular_session_open(now)
    assert regular_session_open(now, lambda day: True)
    assert not regular_session_open(now.replace(tzinfo=None), lambda day: True)
    assert not regular_session_open(now, lambda day: False)
    assert not regular_session_open(datetime(2026, 9, 9, 6, 30, tzinfo=UTC), lambda day: True)
