"""Shared lifecycle and safety boundary; adapters only translate vendor calls."""

from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

from pydantic import ValidationError

from pykbroker.exceptions import (
    BrokerError,
    BrokerRejectedError,
    ConfigurationError,
    InvalidResponseError,
    NotSupportedError,
    TransportError,
)
from pykbroker.models import Balance, CancelRequest, Capabilities, Order, OrderRequest, Position, Quote, Rules
from pykbroker.rules.kr import LOT, equity_rules, normalize_symbol
from pykbroker.safety.gate import OrderSafety

T = TypeVar("T")


def field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise InvalidResponseError("Missing or invalid numeric field")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise InvalidResponseError("Invalid numeric field") from None
    if not result.is_finite() or result < 0:
        raise InvalidResponseError("Nonfinite or negative numeric field")
    return result


def whole(value: Any) -> int:
    number = decimal(value)
    if number != number.to_integral_value():
        raise InvalidResponseError("Fractional stock quantity in response")
    return int(number)


def optional_price(value: Any) -> Decimal | None:
    if value in (None, "", "0", 0):
        return None
    return decimal(value)


def check_response(value: Any, broker: str) -> Any:
    envelope = field(value, "json_data", value)
    code = field(envelope, "rt_cd" if broker == "kis" else "rsp_cd")
    if code is not None and str(code) not in ({"0"} if broker == "kis" else {"00000", "0"}):
        raise BrokerRejectedError("Broker rejected the request; inspect vendor status securely")
    return envelope


class BaseBroker:
    broker_code = "base"
    # Mock (virtual) trading exists only where the vendor offers a separate domain.
    supports_virtual = False

    def __init__(
        self,
        *,
        account: str,
        safety: OrderSafety | None = None,
        market: str = "KR",
        virtual: bool = False,
        halt_provider: Callable[[str], bool] | None = None,
    ) -> None:
        if not isinstance(account, str) or not account.strip():
            raise ConfigurationError("Supply an account identifier")
        if market not in {"KR", "US"}:
            raise NotSupportedError("Supported stock markets: KR, US")
        # Truthy values never promote or demote an account mode.
        if not isinstance(virtual, bool):
            raise ConfigurationError("virtual must be a boolean; the account mode is never inferred")
        if virtual and not self.supports_virtual:
            raise NotSupportedError("This broker has no mock (virtual) trading domain")
        if halt_provider is not None and not callable(halt_provider):
            raise ConfigurationError("halt_provider must be callable")
        self._account = account
        self.market = market
        self.virtual = virtual
        self._halt_provider = halt_provider
        self.safety = safety if safety is not None else OrderSafety()
        self._closed = False

    def _call(self, function: Callable[[], T]) -> T:
        if self._closed:
            raise ConfigurationError("Broker is closed")
        try:
            return function()
        except BrokerError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError, ValidationError):
            raise InvalidResponseError("Vendor response or client contract is invalid") from None
        except Exception:
            raise TransportError("Vendor call failed; reconcile uncertain orders before retrying") from None

    def _symbol(self, symbol: str, market: str) -> str:
        if market != self.market:
            raise NotSupportedError("Request market must match this broker instance")
        return normalize_symbol(symbol) if market == "KR" else symbol

    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_virtual_account=self.supports_virtual,
            virtual=self.virtual,
            requires_venue=self.market == "US",
            venues=("KRX",) if self.market == "KR" else (),
        )

    def _halt_from_provider(self, symbol: str) -> bool | None:
        """A caller-supplied halt answer fills a venue silence; anything but a bool stays unknown."""
        if self._halt_provider is None:
            return None
        answer = self._halt_provider(symbol)
        return answer if isinstance(answer, bool) else None

    def get_rules(
        self,
        symbol: str,
        *,
        now: datetime | None = None,
        trading_day: Callable[[date], bool] | None = None,
    ) -> Rules:
        if self.market == "KR":
            return equity_rules(symbol, halted=self._halt_from_provider(symbol), now=now, trading_day=trading_day)
        # US ticks are venue- and price-dependent and the daily band is intraday (LULD): unknown, not absent.
        return Rules(
            symbol=symbol,
            market="US",
            currency="USD",
            timezone="America/New_York",
            lot=LOT,
            regular_open="09:30",
            regular_close="16:00",
            halted=self._halt_from_provider(symbol),
        )

    def available_cash(self) -> Decimal:
        """Cash the venue will let the next order spend. Re-read every call: fills settle late."""
        return self.get_balance().cash_available

    def available_qty(self, symbol: str) -> int:
        """Shares the venue will let the next order sell. Not held means nothing to sell."""
        code = self._symbol(symbol, self.market)
        for position in self.get_positions():
            if position.symbol == code:
                return position.sellable
        return 0

    def _validate_order(self, request: OrderRequest | CancelRequest) -> None:
        self._symbol(request.symbol, request.market)
        capabilities = self.capabilities()
        # capabilities() is the plan the executor makes; validation must agree with it exactly.
        if request.venue == "NXT" and not capabilities.supports_nxt:
            raise NotSupportedError("NXT is not supported")
        if request.venue is not None:
            if request.venue not in capabilities.venues:
                raise NotSupportedError("Unknown venue for this broker and market")
            if capabilities.requires_venue and request.venue != getattr(self, "venue", None):
                raise NotSupportedError("Order venue must match the configured venue")
        if isinstance(request, OrderRequest) and request.price is not None:
            if self.market == "KR" and request.price != request.price.to_integral_value():
                raise ConfigurationError("KR prices must be whole won; round explicitly before submission")
        if isinstance(request, OrderRequest) and request.order_type == "market":
            if not capabilities.supports_market_order:
                raise NotSupportedError("Market orders are not supported")
        if isinstance(request, OrderRequest) and request.order_type == "limit":
            if not capabilities.supports_limit_order:
                raise NotSupportedError("Limit orders are not supported")

    def place_order(self, request: OrderRequest) -> Order:
        # Revalidate even models constructed with model_construct/model_copy.
        request = OrderRequest.model_validate(request.model_dump())
        self._validate_order(request)
        return self.safety.execute(
            self.broker_code, self._account, request, lambda: self._call(lambda: self._place_order(request))
        )

    def cancel_order(self, request: CancelRequest) -> Order:
        request = CancelRequest.model_validate(request.model_dump())
        self._validate_order(request)
        return self.safety.execute(
            self.broker_code, self._account, request, lambda: self._call(lambda: self._cancel_order(request))
        )

    def _place_order(self, request: OrderRequest) -> Order:
        raise NotSupportedError("Order submission is not implemented")

    def _cancel_order(self, request: CancelRequest) -> Order:
        raise NotSupportedError("Single-order cancellation is not implemented")

    def get_quote(self, symbol: str, *, market: str = "KR") -> Quote:
        raise NotSupportedError("Quotes are not implemented")

    def get_balance(self) -> Balance:
        raise NotSupportedError("Balance is not implemented")

    def get_positions(self) -> list[Position]:
        raise NotSupportedError("Positions are not implemented")

    def list_orders(self, symbol: str | None = None) -> list[Order]:
        raise NotSupportedError("Order listing is not implemented")

    def status(self, order: Order) -> Order:
        for candidate in self.list_orders(order.symbol):
            if candidate.id == order.id:
                return candidate
        # Missing from an open-order list never proves a fill or cancellation.
        return order.model_copy(update={"status": "unknown"})

    def quote(self, symbol: str, *, market: str = "KR") -> Quote:
        return self.get_quote(symbol, market=market)

    def balance(self) -> Balance:
        return self.get_balance()

    def positions(self) -> list[Position]:
        return self.get_positions()

    def rules(
        self,
        symbol: str,
        *,
        now: datetime | None = None,
        trading_day: Callable[[date], bool] | None = None,
    ) -> Rules:
        return self.get_rules(symbol, now=now, trading_day=trading_day)

    def place(self, request: OrderRequest) -> Order:
        return self.place_order(request)

    def cancel(self, request: CancelRequest) -> Order:
        return self.cancel_order(request)

    def close(self) -> None:
        self._closed = True

    def __enter__(self):
        if self._closed:
            raise ConfigurationError("Broker is closed")
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
