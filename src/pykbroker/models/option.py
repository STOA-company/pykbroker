"""IBKR option read models. Not ``Quote``: an option bid of 0.00 is real, and a missing value is ``None``."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from ._base import Model


class OptionContract(Model):
    conid: int
    symbol: str
    right: Literal["C", "P"]
    strike: Decimal
    maturity: date | None = None  # secdef/info maturityDate parsed as YYYYMMDD; None when absent/unparseable
    maturity_raw: str | None = None
    exchange: str | None = None
    currency: str | None = None


class OptionQuote(Model):
    """One snapshot row. ``None`` = IB sent no value for that field (never replaced by 0)."""

    conid: int
    last: Decimal | None = None
    last_is_prev_close: bool = False  # field 31 prefix "C" (OpenAPI field list)
    halted: bool = False  # field 31 prefix "H" (OpenAPI field list)
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None
    volume: Decimal | None = None
    open_interest: Decimal | None = None
    iv_pct: Decimal | None = None  # field 7633, percent as sent
    delta: Decimal | None = None  # field 7308
    availability: str | None = None  # field 6509, e.g. "RpB"; first char N = not subscribed
    has_data: bool = False  # False = IB only acknowledged the request (subscription starting)
    unverified: tuple[str, ...] = ()  # field numbers whose text has no OpenAPI-backed parse; raw kept in `raw`
    raw: dict[str, str] = {}


class Bar(Model):
    time: datetime  # UTC, start of the bar (OpenAPI: "Unix timestamp of the start of the bar", ms)
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None
