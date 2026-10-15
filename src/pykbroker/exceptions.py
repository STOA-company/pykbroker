"""Stable errors; vendor payloads and credentials never enter messages."""


class BrokerError(Exception):
    """Base SDK error."""


class ConfigurationError(BrokerError):
    """Required caller configuration is missing or invalid."""


class MissingDependencyError(ConfigurationError):
    """Install the optional vendor SDK or inject a client."""


class NotSupportedError(BrokerError):
    """The requested capability has not been implemented."""


class SpecUnverifiedError(NotSupportedError):
    """A specification lock prevents this operation."""


class FractionalNotSupportedError(NotSupportedError):
    """Fractional shares (amount buy / decimal sell) are not an API feature of this broker/market.

    Raised before any HTTP. Never resolved by rounding or truncating the quantity.
    """

    code = "FRACTIONAL_NOT_SUPPORTED"

    def __init__(self, detail: str = "") -> None:
        super().__init__("Fractional orders are not supported here" + (f": {detail}" if detail else ""))


class FractionalDisabledError(NotSupportedError):
    """FRACTIONAL_MARKET_ALLOWED is switched off: fractional/amount orders are refused before HTTP."""

    code = "FRACTIONAL_DISABLED"

    def __init__(self) -> None:
        super().__init__("소수점 주문은 아직 켜지지 않았어요")


class FractionalOutsideSessionError(NotSupportedError):
    """Toss accepts fractional/amount orders only in the US regular session (ET 09:30-15:00)."""

    code = "FRACTIONAL_OUTSIDE_SESSION"

    def __init__(self) -> None:
        super().__init__("Fractional orders are accepted only during the US regular session window")


class InvalidResponseError(BrokerError):
    """A vendor response cannot be interpreted safely.

    On an order write this means the outcome is unknown, not rejected: the order may have reached
    the venue. Reconcile against the venue's order records before submitting it again.
    """


class BrokerRejectedError(BrokerError):
    """The vendor explicitly rejected the request."""


class TossRejectedError(BrokerRejectedError):
    """Toss 4xx rejection with a sanitized vendor code only."""

    def __init__(self, code: str | None = None) -> None:
        self.code = code
        super().__init__("Toss rejected the request")


class TransportError(BrokerError):
    """Outcome may be unknown; reconcile before any new submission."""


class SafetyError(BrokerError):
    """A safety gate blocked an operation."""


class DryRunError(SafetyError):
    """Live submission is disabled; no vendor mutation occurred."""


class KillSwitchError(SafetyError):
    """Caller halt status is missing, unreadable, or halted."""


class RiskCapError(SafetyError):
    """A supplied risk cap is missing or would be exceeded."""


class DuplicateOrderError(SafetyError):
    """This client key has already been claimed."""


class CircuitOpenError(SafetyError):
    """Caller breaker state does not permit submission."""
