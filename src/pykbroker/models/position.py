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

    @model_validator(mode="after")
    def _sellable_cannot_exceed_the_holding(self) -> "Position":
        # Believing such a row would place a sell order for shares the account does not hold.
        if self.sellable > self.quantity:
            raise ValueError("Sellable quantity exceeds the holding")
        return self
