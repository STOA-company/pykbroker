from datetime import datetime

from ._base import Model, PositiveMoney


class Quote(Model):
    symbol: str
    market: str = "KR"
    last: PositiveMoney
    bid: PositiveMoney | None = None
    ask: PositiveMoney | None = None
    prev_close: PositiveMoney | None = None
    timestamp: datetime | None = None
