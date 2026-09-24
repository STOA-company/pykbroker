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


class InvalidResponseError(BrokerError):
    """A vendor response cannot be interpreted safely."""


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
