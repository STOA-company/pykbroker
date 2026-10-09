"""Toss US fractional holdings: one fractional row must not fail the whole position read.

Toss US keeps fractional shares (amount buys) and sells a fraction only by a market order, so the
order path sees whole shares while the exact holding stays visible. Injected SDK; no network.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from pykbroker import Toss
from pykbroker.exceptions import InvalidResponseError
from pykbroker.models import Position


def _row(symbol, quantity, market="US", last="200", value="500"):
    return {"symbol": symbol, "name": symbol, "marketCountry": market, "quantity": quantity,
            "averagePurchasePrice": "150", "lastPrice": last, "marketValue": {"amount": value}}


class _SDK:
    def __init__(self, rows, sellable, **options):
        self.rows = rows
        self.sellable = sellable
        self.options = options

    async def get_positions(self):
        return {"marketValue": {"amount": {"usd": "0", "krw": "0"}}, "items": self.rows}

    async def get_sellable_quantity(self, symbol):
        return {"sellableQuantity": self.sellable[symbol]}

    async def aclose(self):
        return None


def _broker(rows, sellable, market="US"):
    options = {"allow_us_pilot": True} if market == "US" else {}
    return Toss(account="fixture", account_seq=7, market=market,
                sdk_factory=lambda **kw: _SDK(rows, sellable, **kw), **options)


def test_us_fractional_rows_read_as_whole_shares_with_the_exact_holding():
    broker = _broker(
        [_row("AAPL", "2.5"), _row("TSLA", "0.23"), _row("MSFT", "3"), _row("005930", "4", market="KR")],
        {"AAPL": "2.5", "TSLA": "0.23", "MSFT": "1"})
    try:
        positions = {p.symbol: p for p in broker.get_positions()}
    finally:
        broker.close()
    assert set(positions) == {"AAPL", "TSLA", "MSFT"}
    aapl, tsla, msft = positions["AAPL"], positions["TSLA"], positions["MSFT"]
    assert (aapl.quantity, aapl.sellable, aapl.quantity_exact) == (2, 2, Decimal("2.5"))
    # Below one share: the row stays (so the holding is not hidden) but nothing is limit-sellable.
    assert (tsla.quantity, tsla.sellable, tsla.quantity_exact) == (0, 0, Decimal("0.23"))
    assert (msft.quantity, msft.sellable, msft.quantity_exact) == (3, 1, None)
    assert aapl.model_dump(mode="json")["quantity_exact"] == "2.5"
    assert msft.model_dump(mode="json")["quantity_exact"] is None


def test_us_whole_share_rows_keep_reading_exactly_as_before():
    broker = _broker([_row("AAPL", "4")], {"AAPL": "3"})
    try:
        (aapl,) = broker.get_positions()
    finally:
        broker.close()
    assert (aapl.quantity, aapl.sellable, aapl.quantity_exact) == (4, 3, None)


def test_us_sellable_above_the_exact_holding_is_still_an_invalid_response():
    broker = _broker([_row("AAPL", "2.4")], {"AAPL": "2.5"})
    try:
        with pytest.raises(InvalidResponseError):
            broker.get_positions()
    finally:
        broker.close()


def test_kr_fractional_quantity_stays_an_invalid_response():
    broker = _broker([_row("005930", "1.5", market="KR", last="70000", value="105000")], {"005930": "1"},
                     market="KR")
    try:
        with pytest.raises(InvalidResponseError):
            broker.get_positions()
    finally:
        broker.close()


def _position(**kwargs):
    options = dict(symbol="AAPL", market="US", quantity=2, sellable=2, average_price="150", last="200",
                   market_value="500")
    options.update(kwargs)
    return Position(**options)


def test_exact_quantity_only_explains_a_fractional_remainder():
    assert _position(quantity_exact="2.5").quantity_exact == Decimal("2.5")
    assert _position().quantity_exact is None
    for wrong in ("2", "3.5", "1.9", "-0.5"):
        with pytest.raises(ValidationError):
            _position(quantity_exact=wrong)
