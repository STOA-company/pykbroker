from pydantic import model_validator

from ._base import Model


class Capabilities(Model):
    supports_spot: bool = False
    supports_market_order: bool = False
    supports_single_order_cancel: bool = False
    supports_nxt: bool = False
    supports_order_history: bool = False
    # True only where the venue answers an unfilled-order snapshot in real time.
    # A daily ledger read back for today (the KIS mock domain) is not one: it lags.
    realtime_open_orders: bool = False
    # US venues have no market order on either broker; the executor reads this and goes limit.
    supports_limit_order: bool = True
    # Fractional shares (Toss US only): amount-based MARKET BUY / decimal-quantity MARKET SELL.
    # Both default to False so every other broker refuses a fractional request before any HTTP.
    supports_amount_market_buy: bool = False
    supports_fractional_market_sell: bool = False
    # True when an order cannot be routed without naming one of `venues`.
    requires_venue: bool = False
    venues: tuple[str, ...] = ()
    # Mock (virtual) trading is opt-in per broker; an unsupported broker never pretends.
    supports_virtual_account: bool = False
    virtual: bool = False
    markets: tuple[str, ...] = ()
    # Optional order shapes. Defaults refuse: a broker opts in by declaring them.
    supports_stop_order: bool = False
    supports_stop_limit_order: bool = False
    time_in_force: tuple[str, ...] = ()
    supports_amend: bool = False
    # Asset classes the order path is open for (Saxo AssetType names); empty = not asset-typed.
    asset_types: tuple[str, ...] = ()
    # Asset classes the broker sells that this build keeps closed (capability column only).
    asset_types_closed: tuple[str, ...] = ()
    # Order currency per market, e.g. {"US": "USD", "SG": "SGD"} (data, not a branch).
    currency: tuple[tuple[str, str], ...] = ()

    @model_validator(mode="after")
    def _venue_requirement_names_its_venues(self) -> "Capabilities":
        # 「거래소를 대라」와 「댈 거래소가 없다」가 동시에 참이면 그 브로커는 주문도 취소도
        # 전부 거절한다. 모순은 쓰이는 순간이 아니라 만들어지는 순간에 막는다.
        if self.requires_venue and not self.venues:
            raise ValueError("requires_venue=True must declare at least one venue")
        if any(not v.strip() for v in self.venues):
            raise ValueError("venues must not contain empty or whitespace-only names")
        return self
