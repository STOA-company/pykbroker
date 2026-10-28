from decimal import Decimal

from ._base import Model, Money


class Balance(Model):
    currency: str = "KRW"
    cash_available: Money
    position_value: Money
    orderable_cash: Decimal | None = None

    @property
    def total(self) -> Money:
        return self.cash_available + self.position_value
