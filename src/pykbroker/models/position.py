from decimal import ROUND_FLOOR

from pydantic import model_validator

from ._base import Model, Money, Quantity


class Position(Model):
    symbol: str
    name: str = ""
    market: str = "KR"
    quantity: Quantity
    sellable: Quantity
    average_price: Money
    last: Money
    market_value: Money
    # quantity/sellable are whole shares, the unit a limit order can sell. A broker that keeps a
    # fractional remainder (Toss US) reports the exact holding here; None means the holding is whole.
    quantity_exact: Money | None = None

    @model_validator(mode="after")
    def _sellable_cannot_exceed_the_holding(self) -> "Position":
        # Believing such a row would place a sell order for shares the account does not hold.
        if self.sellable > self.quantity:
            raise ValueError("Sellable quantity exceeds the holding")
        exact = self.quantity_exact
        if exact is not None and (exact == self.quantity
                                  or exact.to_integral_value(rounding=ROUND_FLOOR) != self.quantity):
            # The exact value only ever explains a fractional remainder behind the whole quantity.
            raise ValueError("Exact quantity must be the fractional holding behind the whole quantity")
        return self
