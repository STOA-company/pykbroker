from typing import Literal, Self

from pydantic import Field, model_validator

from ._base import Model, Money, PositiveMoney, PositiveQuantity, Quantity

Side = Literal["buy", "sell"]
OrderType = Literal["limit", "market", "close"]
OrderStatus = Literal["accepted", "open", "partial", "filled", "cancelled", "rejected", "unknown"]


class OrderRequest(Model):
    symbol: str = Field(min_length=1)
    side: Side
    quantity: PositiveQuantity
    order_type: OrderType = "limit"
    price: PositiveMoney | None = None
    market: Literal["KR", "US"] = "KR"
    venue: str | None = None
    client_key: str = Field(min_length=1, max_length=128, pattern=r"\S", repr=False)

    @model_validator(mode="after")
    def valid_price(self) -> Self:
        if self.order_type == "limit" and self.price is None:
            raise ValueError("A limit order requires a positive price")
        if self.order_type == "market" and self.price is not None:
            raise ValueError("A market order cannot carry a limit price")
        if not self.client_key.strip():
            raise ValueError("client_key cannot be blank")
        return self


class Order(Model):
    id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: Side | None = None
    order_type: OrderType | None = None
    quantity: Quantity | None = None
    price: Money | None = None
    # Broker-reported average execution price; never the submitted limit price.
    # None when execution pricing has not been supplied by the broker.
    fill_price: PositiveMoney | None = None
    filled: Quantity | None = None
    remaining: Quantity | None = None
    status: OrderStatus = "unknown"
    market: str = "KR"
    # True only when the submission went to a broker mock (virtual) account.
    virtual: bool = False


class AmendRequest(OrderRequest):
    """Change a positive quantity to a new limit price; support is adapter-specific.

    The side describes the caller's original intent for risk valuation. A broker acknowledgement that
    does not echo the original side must not turn it into a broker-confirmed side.
    """

    order_id: str = Field(min_length=1)
    order_type: Literal["limit"] = "limit"


class CancelRequest(Model):
    order_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    quantity: PositiveQuantity
    client_key: str = Field(min_length=1, max_length=128, pattern=r"\S", repr=False)
    market: Literal["KR", "US"] = "KR"
    venue: str | None = None
