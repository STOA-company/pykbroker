from ._base import Model, PositiveMoney, PositiveQuantity


class Rules(Model):
    """What the venue permits for one symbol. None always means «not known», never «no».

    An executor must treat an unknown session or halt exactly like a closed or halted
    market: `tradable` is the only field it should branch on.
    """

    symbol: str
    market: str = "KR"
    currency: str = "KRW"
    tick: PositiveMoney | None = None
    lot: PositiveQuantity = 1
    min_notional: PositiveMoney | None = None
    regular_open: str | None = None
    regular_close: str | None = None
    timezone: str = "Asia/Seoul"
    # Tri-state: True open, False closed, None unknown (no calendar or no clock supplied).
    session_open: bool | None = None
    # Tri-state: True halted, False trading, None unknown (the venue did not say).
    halted: bool | None = None
    limit_up: PositiveMoney | None = None
    limit_down: PositiveMoney | None = None

    @property
    def tradable(self) -> bool:
        """Only an affirmatively open session on an affirmatively unhalted symbol."""
        return self.session_open is True and self.halted is False
