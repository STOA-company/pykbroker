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
