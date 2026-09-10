from ._base import Model


class Capabilities(Model):
    supports_spot: bool = False
    supports_market_order: bool = False
    supports_single_order_cancel: bool = False
    supports_nxt: bool = False
    supports_order_history: bool = False
    # US venues have no market order on either broker; the executor reads this and goes limit.
    supports_limit_order: bool = True
    # True when an order cannot be routed without naming one of `venues`.
    requires_venue: bool = False
    venues: tuple[str, ...] = ()
    # Mock (virtual) trading is opt-in per broker; an unsupported broker never pretends.
    supports_virtual_account: bool = False
    virtual: bool = False
    markets: tuple[str, ...] = ()
