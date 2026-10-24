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
    # Exact holding as the broker reports it. `quantity`/`sellable` stay whole shares (floored) so
    # integer-only consumers and the integer order path keep working; a fractional holding
    # (US fractional shares on Toss) shows its remainder only here. None = broker has no fractions.
    quantity_exact: Money | None = None
    sellable_exact: Money | None = None

    @model_validator(mode="after")
    def _sellable_cannot_exceed_the_holding(self) -> "Position":
        # Believing such a row would place a sell order for shares the account does not hold.
        if self.sellable > self.quantity:
            raise ValueError("Sellable quantity exceeds the holding")
        if (
            self.quantity_exact is not None
            and self.sellable_exact is not None
            and self.sellable_exact > self.quantity_exact
        ):
            raise ValueError("Sellable quantity exceeds the holding")
        return self
