"""KRX cash-equity defaults. Calendar/venue/product overrides belong to callers."""

import re
from collections.abc import Callable
from datetime import date, datetime, time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from zoneinfo import ZoneInfo

from pykbroker.exceptions import ConfigurationError
from pykbroker.models import Rules

# KRX cash equities, checked 2026-09-09; source links in docs/api/rules.md.
TICK_BANDS = ((2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500))
REGULAR_OPEN = time(9)
REGULAR_CLOSE = time(15, 30)
KST = ZoneInfo("Asia/Seoul")


def normalize_symbol(symbol: str) -> str:
    if not isinstance(symbol, str):
        raise ConfigurationError("KR symbol must be a six-character code")
    value = symbol.removeprefix("A") if len(symbol) == 7 else symbol
    # KRX short codes may contain letters; no company-specific symbol aliases.
    if not re.fullmatch(r"[0-9A-Z]{6}", value):
        raise ConfigurationError("KR symbol must be a six-character code")
    return value


def tick_size(price: Decimal | int | str) -> Decimal:
    value = Decimal(str(price))
    if not value.is_finite() or value <= 0:
        raise ConfigurationError("Price must be positive and finite")
    return Decimal(next((step for upper, step in TICK_BANDS if value < upper), 1000))


def round_price(price: Decimal | int | str, side: str) -> Decimal:
    if side not in {"buy", "sell"}:
        raise ConfigurationError("Side must be buy or sell")
    value = Decimal(str(price))
    tick = tick_size(value)
    rounded = (value / tick).to_integral_value(rounding=ROUND_FLOOR if side == "buy" else ROUND_CEILING) * tick
    return rounded


def regular_session_open(now: datetime, trading_day: Callable[[date], bool] | None = None) -> bool:
    """No calendar means closed; weekdays alone do not establish trading days."""
    if now.tzinfo is None or trading_day is None:
        return False
    local = now.astimezone(KST)
    return trading_day(local.date()) is True and REGULAR_OPEN <= local.time() < REGULAR_CLOSE


def equity_rules(symbol: str, price: Decimal | None = None) -> Rules:
    return Rules(
        symbol=normalize_symbol(symbol),
        tick=tick_size(price) if price is not None else None,
        regular_open="09:00",
        regular_close="15:30",
    )
