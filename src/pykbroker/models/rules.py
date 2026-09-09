from ._base import Model, PositiveMoney, PositiveQuantity


class Rules(Model):
    symbol: str
    market: str = "KR"
    currency: str = "KRW"
    tick: PositiveMoney | None = None
    lot: PositiveQuantity = 1
    min_notional: PositiveMoney | None = None
    regular_open: str | None = None
    regular_close: str | None = None
    timezone: str = "Asia/Seoul"
