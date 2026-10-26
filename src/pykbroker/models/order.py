from typing import Literal, Self

from pydantic import Field, model_validator

from ._base import Model, Money, PositiveMoney, PositiveQuantity, Quantity

Side = Literal["buy", "sell"]
OrderType = Literal["limit", "market", "close"]
# "overnight" = US 주간거래(daytime) — the customer picked it; an adapter must declare it in
# Capabilities.sessions or the request is refused before any HTTP.
OrderSession = Literal["regular", "overnight"]
OrderStatus = Literal["accepted", "open", "partial", "filled", "cancelled", "rejected", "unknown"]


class OrderRequest(Model):
    symbol: str = Field(min_length=1)
    side: Side
    quantity: PositiveQuantity
    order_type: OrderType = "limit"
    price: PositiveMoney | None = None
    market: Literal["KR", "US"] = "KR"
    venue: str | None = None
    session: OrderSession = "regular"
    client_key: str = Field(min_length=1, max_length=128, pattern=r"\S", repr=False)

    @model_validator(mode="after")
    def valid_session(self) -> Self:
        # 주간거래는 미국 지정가만(KIS·NH 공식 사양: 주간 = 지정가만).
        if self.session == "overnight" and (
            self.market != "US" or self.order_type != "limit"
        ):
            raise ValueError("An overnight order is a whole-share US LIMIT order only")
        return self

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


class CancelRequest(Model):
    order_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    quantity: PositiveQuantity
    client_key: str = Field(min_length=1, max_length=128, pattern=r"\S", repr=False)
    market: Literal["KR", "US"] = "KR"
    venue: str | None = None
    session: OrderSession = "regular"


class AmendRequest(Model):
    """Price-only amendment of one open limit order. Quantity is the order's remaining quantity and
    never changes (Toss US modifies price only; NH modify carries a price only)."""

    order_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: Side
    quantity: PositiveQuantity
    price: PositiveMoney
    client_key: str = Field(min_length=1, max_length=128, pattern=r"\S", repr=False)
    market: Literal["KR", "US"] = "US"
    venue: str | None = None
    session: OrderSession = "regular"
