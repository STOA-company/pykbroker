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
# KRX daily band for cash equities: the previous close plus or minus 30 percent.
DAILY_LIMIT_RATE = Decimal("0.30")
# KRX cash equities trade in whole shares; there is no fractional lot on the order path.
LOT = 1


def normalize_symbol(symbol: str) -> str:
    if not isinstance(symbol, str):
        raise ConfigurationError("KR symbol must be a six-character code")
    value = symbol.removeprefix("A") if len(symbol) == 7 else symbol
    # KRX short codes may contain letters; no company-specific symbol aliases.
    if not re.fullmatch(r"[0-9A-Z]{6}", value):
        raise ConfigurationError("KR symbol must be a six-character code")
    return value


def _price(value: Decimal | int | str) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ConfigurationError("Price must be positive and finite")
    return result


def tick_size(price: Decimal | int | str) -> Decimal:
    value = _price(price)
    return Decimal(next((step for upper, step in TICK_BANDS if value < upper), 1000))


def round_price(price: Decimal | int | str, side: str) -> Decimal:
    if side not in {"buy", "sell"}:
        raise ConfigurationError("Side must be buy or sell")
    value = Decimal(str(price))
    tick = tick_size(value)
    rounded = (value / tick).to_integral_value(rounding=ROUND_FLOOR if side == "buy" else ROUND_CEILING) * tick
    return rounded


def price_limits(prev_close: Decimal | int | str) -> tuple[Decimal, Decimal]:
    """Daily upper and lower bound, each rounded *inward* to the tick that applies there.

    Rounding outward would produce a price the venue refuses, so the band never widens.
    """
    base = _price(prev_close)
    raw_up, raw_down = base * (1 + DAILY_LIMIT_RATE), base * (1 - DAILY_LIMIT_RATE)
    up_tick, down_tick = tick_size(raw_up), tick_size(raw_down)
    upper = (raw_up / up_tick).to_integral_value(rounding=ROUND_FLOOR) * up_tick
    lower = (raw_down / down_tick).to_integral_value(rounding=ROUND_CEILING) * down_tick
    return upper, lower


def session_state(now: datetime | None, trading_day: Callable[[date], bool] | None = None) -> bool | None:
    """True open, False closed, None unknown. A weekday alone never establishes a trading day."""
    if now is None or now.tzinfo is None or trading_day is None:
        return None
    local = now.astimezone(KST)
    if trading_day(local.date()) is not True:
        return False
    return REGULAR_OPEN <= local.time() < REGULAR_CLOSE


def regular_session_open(now: datetime, trading_day: Callable[[date], bool] | None = None) -> bool:
    """Fail-closed view of `session_state`: an unknown session is not an open one."""
    return session_state(now, trading_day) is True


def daily_band(
    limit_up: Decimal | int | str | None,
    limit_down: Decimal | int | str | None,
    prev_close: Decimal | int | str | None,
) -> tuple[Decimal | None, Decimal | None, str | None]:
    """The venue's own band when it stated one; the ±30% rule only when it did not.

    `price_limits` is a KRX cash-equity default. ETF·ETN, 정리매매 and 신규상장 do not
    share it, so a stated band always wins and is never re-derived or widened.
    """
    stated = (limit_up is not None, limit_down is not None)
    if any(stated) and not all(stated):
        raise ConfigurationError("A daily band needs both sides; half a band is not a band")
    if all(stated):
        upper, lower = _price(limit_up), _price(limit_down)
        if lower > upper:
            raise ConfigurationError("Daily band lower bound is above the upper bound")
        return upper, lower, "venue"
    if prev_close is None:
        return None, None, None
    upper, lower = price_limits(prev_close)
    return upper, lower, "derived"


def equity_rules(
    symbol: str,
    price: Decimal | None = None,
    prev_close: Decimal | None = None,
    *,
    halted: bool | None = None,
    now: datetime | None = None,
    trading_day: Callable[[date], bool] | None = None,
    limit_up: Decimal | None = None,
    limit_down: Decimal | None = None,
) -> Rules:
    upper, lower, source = daily_band(limit_up, limit_down, prev_close)
    return Rules(
        symbol=normalize_symbol(symbol),
        tick=tick_size(price) if price is not None else None,
        lot=LOT,
        # One whole share is the smallest thing a KR buyer can pay for.
        min_notional=_price(price) * LOT if price is not None else None,
        regular_open="09:00",
        regular_close="15:30",
        session_open=session_state(now, trading_day),
        halted=halted if isinstance(halted, bool) else None,
        limit_up=upper,
        limit_down=lower,
        limits_source=source,
    )
