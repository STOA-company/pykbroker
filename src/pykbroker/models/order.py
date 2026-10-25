from decimal import Decimal
from typing import Literal, Self

from pydantic import Field, model_validator

from ._base import FractionalQuantity, Model, Money, PositiveMoney, PositiveQuantity, Quantity

Side = Literal["buy", "sell"]
# limit/market/close are the shared kinds (close = after-hours close session: KIS 장후 시간외, DB "81", Toss CLS).
# The rest are priced-by-venue kinds an adapter must declare in Capabilities.order_types before any HTTP.
OrderType = Literal["limit", "market", "close", "conditional_limit", "best", "top", "mid", "stop_limit"]
TimeInForce = Literal["day", "ioc", "fok"]
OrderSession = Literal["regular", "pre", "single"]
PRICED_TYPES = ("limit", "conditional_limit", "stop_limit")
OrderStatus = Literal["accepted", "open", "partial", "filled", "cancelled", "rejected", "unknown"]


class OrderRequest(Model):
    symbol: str = Field(min_length=1)
    side: Side
    # Whole shares everywhere; a fractional Decimal only for Toss US MARKET SELL.
    # Exactly one of `quantity` / `amount` is set (Toss OpenAPI 1.2.24 oneOf).
    quantity: PositiveQuantity | FractionalQuantity | None = None
    # Amount-based buy (Toss US `orderAmount`): USD, at most 2 decimal places, MARKET BUY only.
    amount: PositiveMoney | None = None
    order_type: OrderType = "limit"
    price: PositiveMoney | None = None
    market: Literal["KR", "US"] = "KR"
    venue: str | None = None
    # Defaults are the plain day order in the regular session; anything else must be declared by the adapter.
    time_in_force: TimeInForce = "day"
    session: OrderSession = "regular"
    # Trigger price of a stop_limit order only.
    stop_price: PositiveMoney | None = None
    client_key: str = Field(min_length=1, max_length=128, pattern=r"\S", repr=False)

    @property
    def fractional(self) -> bool:
        """True when this request needs a fractional-share path (amount buy or decimal sell)."""
        return self.amount is not None or (
            isinstance(self.quantity, Decimal) and self.quantity != self.quantity.to_integral_value()
        )

    @model_validator(mode="after")
    def valid_quantity_or_amount(self) -> Self:
        if (self.quantity is None) == (self.amount is None):
            raise ValueError("Exactly one of quantity or amount is required")
        if self.amount is not None:
            if self.order_type != "market" or self.side != "buy" or self.market != "US":
                raise ValueError("An amount-based order is a US MARKET BUY only")
            if self.amount != self.amount.quantize(Decimal("0.01")):
                raise ValueError("An amount-based order carries at most 2 decimal places (USD)")
        if isinstance(self.quantity, Decimal):
            if self.quantity == self.quantity.to_integral_value():
                # A whole Decimal is a whole-share order; keep the integer contract for adapters.
                object.__setattr__(self, "quantity", int(self.quantity))
            else:
                if self.order_type != "market" or self.side != "sell" or self.market != "US":
                    raise ValueError("A fractional quantity is a US MARKET SELL only")
                if self.quantity != self.quantity.quantize(Decimal("0.000001")):
                    raise ValueError("A fractional quantity carries at most 6 decimal places")
        return self

    @model_validator(mode="after")
    def valid_price(self) -> Self:
        if self.order_type in PRICED_TYPES and self.price is None:
            raise ValueError("A limit order requires a positive price")
        if self.order_type in ("market", "best", "top", "mid") and self.price is not None:
            raise ValueError("A market-style order cannot carry a limit price")
        if (self.order_type == "stop_limit") != (self.stop_price is not None):
            raise ValueError("stop_price is required for, and only for, a stop_limit order")
        if self.order_type == "close" and (self.time_in_force != "day" or self.session != "regular"):
            raise ValueError("A close-session order is a plain day order")
        if self.session != "regular" and self.time_in_force != "day":
            raise ValueError("An off-session order is a day order")
        if not self.client_key.strip():
            raise ValueError("client_key cannot be blank")
        return self


class Order(Model):
    id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: Side | None = None
    order_type: OrderType | None = None
    quantity: Quantity | None = None
    # Fractional submissions leave `quantity` None (never floored) and carry the exact
    # Decimal here; an amount-based buy records the submitted USD amount in `amount`.
    quantity_exact: Money | None = None
    amount: Money | None = None
    price: Money | None = None
    # Broker-reported average execution price; never the submitted limit price.
    # None when execution pricing has not been supplied by the broker.
    fill_price: PositiveMoney | None = None
    filled: Quantity | None = None
    remaining: Quantity | None = None
    status: OrderStatus = "unknown"
    market: str = "KR"
    # Execution venue the broker reported or the request named (KRX/NXT/SOR); None when not stated.
    venue: str | None = None
    # Echo of the caller's idempotency key (copied from the request) so an unknown outcome can be reconciled by key.
    client_key: str | None = Field(default=None, max_length=128, repr=False)
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
