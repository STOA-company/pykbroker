"""Shared lifecycle and safety boundary; adapters only translate vendor calls."""

from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple, TypeVar

from pydantic import ValidationError

from pykbroker.exceptions import (
    BrokerError,
    BrokerRejectedError,
    ConfigurationError,
    FractionalNotSupportedError,
    InvalidResponseError,
    NotSupportedError,
    SpecUnverifiedError,
    TransportError,
)
from pykbroker.models import (
    AmendRequest,
    Balance,
    CancelRequest,
    Capabilities,
    Order,
    OrderRequest,
    Position,
    Quote,
    Rules,
)
from pykbroker.models.markets import CURRENCIES, INTEGER_PRICE_CURRENCIES, TIMEZONES
from pykbroker.reject_codes import definitive_rejection
from pykbroker.rules.kr import LOT, equity_rules, normalize_symbol
from pykbroker.safety.gate import OrderSafety

T = TypeVar("T")


class OrderLimit(NamedTuple):
    """A broker buying-power read with its unit made explicit.

    ``currency`` is the currency label of ``amount`` ("USD" or "KRW"); ``unit`` is "amount" or
    "quantity". Consumers must never compare a KRW amount against a USD notional.
    """

    amount: Decimal
    currency: str
    unit: str = "amount"


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


def _same_row(left: Any, right: Any) -> bool:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return dict(left) == dict(right)
    return left == right


class PageRows:
    """One holdings list read across vendor pages, each vendor row counted once.

    ``key`` names the fields that identify one position row in that vendor's documented response
    (symbol plus the balance-type / venue / loan fields it carries). A row on a later page whose key
    was already listed on an earlier page is either the identical row again (overlapping or repeated
    page: skipped, so 2 held never reads as 4) or a different row for the same position (the pages
    disagree: refused, so the caller reads «holdings unknown» and sends nothing). Rows within one
    page are kept exactly as the vendor sent them. A duplicate is only ever the identical vendor
    row, never a guess from equal quantities.
    """

    def __init__(self, key: tuple[str, ...], vendor: str) -> None:
        self._key = tuple(key)
        self._vendor = vendor
        self._earlier: dict[tuple[Any, ...], list[Any]] = {}
        self.rows: list[Any] = []

    def add(self, page: Any) -> list[Any]:
        """Rows of this page not already listed on an earlier page (also kept in ``rows``)."""
        fresh: list[Any] = []
        current: dict[tuple[Any, ...], list[Any]] = {}
        for row in page:
            identity = tuple(field(row, name) for name in self._key)
            earlier = self._earlier.get(identity)
            if earlier is not None:
                if any(_same_row(row, seen) for seen in earlier):
                    continue
                raise InvalidResponseError(
                    f"{self._vendor} holdings pages list one position twice with different values; "
                    "refusing an unverifiable total"
                )
            current.setdefault(identity, []).append(row)
            fresh.append(row)
        for identity, rows in current.items():
            self._earlier.setdefault(identity, []).extend(rows)
        self.rows.extend(fresh)
        return fresh


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
    supported_markets = ("KR", "US")
    # Mock (virtual) trading exists only where the vendor offers a separate domain.
    supports_virtual = False
    # US routing needs a named venue, so a broker that has not verified its overseas venue
    # list has nothing to name. Declaring the requirement with an empty list is a contradiction:
    # every order and every cancellation is refused whether or not the caller names a venue.
    us_venues: tuple[str, ...] = ()
    # Internal class-share form is ROOT.CLASS (SEAM §5); a broker whose API spells it otherwise overrides this data
    # cell only (KIS «/»). `canonical_symbol` is the inverse for responses.
    us_class_separator: str = "."

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
        if market not in self.supported_markets:
            raise NotSupportedError("Market is outside this adapter capability declaration")
        # Truthy values never promote or demote an account mode.
        if not isinstance(virtual, bool):
            raise ConfigurationError("virtual must be a boolean; the account mode is never inferred")
        if virtual and not self.supports_virtual:
            raise NotSupportedError("This broker has no mock (virtual) trading domain")
        if halt_provider is not None and not callable(halt_provider):
            raise ConfigurationError("halt_provider must be callable")
        self._account = account
        self.market = market
        self._virtual = virtual
        self._halt_provider = halt_provider
        self.safety = safety if safety is not None else OrderSafety()
        self._closed = False

    @property
    def virtual(self) -> bool:
        return self._virtual

    @virtual.setter
    def virtual(self, value: bool) -> None:
        self._virtual = value

    def _call(self, function: Callable[[], T]) -> T:
        if self._closed:
            raise ConfigurationError("Broker is closed")
        try:
            return function()
        except BrokerError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError, ValidationError) as exc:
            # A vendor answer listed in reject_codes proves «not accepted»; anything else stays unknown.
            definite = definitive_rejection(self.broker_code, exc)
            if definite is not None:
                raise definite from None
            raise InvalidResponseError("Vendor response or client contract is invalid") from None
        except Exception as exc:
            definite = definitive_rejection(self.broker_code, exc)
            if definite is not None:
                raise definite from None
            raise TransportError("Vendor call failed; reconcile uncertain orders before retrying") from None

    def _symbol(self, symbol: str, market: str) -> str:
        if market != self.market:
            raise NotSupportedError("Request market must match this broker instance")
        return (normalize_symbol(symbol) if market == "KR" else
                symbol.replace(".", self.us_class_separator) if market == "US" else symbol)

    def canonical_symbol(self, native: str) -> str:
        """Broker-native US symbol back to the internal ROOT.CLASS form; KR codes are unchanged."""
        return native.replace(self.us_class_separator, ".") if self.market == "US" else native

    def capabilities(self) -> Capabilities:
        if self.market == "US" and not self.us_venues:
            raise SpecUnverifiedError("This broker has not verified its US venues; overseas routing is locked")
        return Capabilities(
            markets=(self.market,),
            supports_virtual_account=self.supports_virtual,
            virtual=self.virtual,
            requires_venue=self.market == "US",
            venues=("KRX",) if self.market == "KR" else self.us_venues,
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
        if self.market not in {"KR", "US"}:
            return Rules(symbol=symbol, market=self.market, currency=CURRENCIES[self.market],
                         timezone=TIMEZONES[self.market],
                         halted=self._halt_from_provider(symbol))
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
            if position.symbol == self.canonical_symbol(code):
                return position.sellable
        return 0

    def _validate_order(self, request: OrderRequest | CancelRequest) -> None:
        self._symbol(request.symbol, request.market)
        capabilities = self.capabilities()
        if capabilities.markets and request.market not in capabilities.markets:
            raise NotSupportedError("Market is outside this adapter capability declaration")
        # capabilities() is the plan the executor makes; validation must agree with it exactly.
        if request.venue == "NXT" and not capabilities.supports_nxt:
            raise NotSupportedError("NXT is not supported")
        # A market that requires a venue gets one named on the request. Falling back to
        # whatever this instance was configured for would route an order nobody reviewed.
        if capabilities.requires_venue and request.venue is None:
            raise NotSupportedError("This market requires the order to name its venue")
        if request.venue is not None:
            if request.venue not in capabilities.venues:
                raise NotSupportedError("Unknown venue for this broker and market")
            if capabilities.requires_venue and request.venue != getattr(self, "venue", None):
                raise NotSupportedError("Order venue must match the configured venue")
        if isinstance(request, CancelRequest) and not capabilities.supports_single_order_cancel:
            raise NotSupportedError("Single-order cancellation is not supported")
        if isinstance(request, OrderRequest):
            currency = capabilities.currency.get(request.market)
            if currency in INTEGER_PRICE_CURRENCIES:
                for price in (request.price, request.stop_price):
                    if price is not None and price != price.to_integral_value():
                        unit = ("KR prices must be whole won" if currency == "KRW"
                                else f"{currency} prices must be whole")
                        raise ConfigurationError(unit + "; round explicitly before submission")
        if isinstance(request, AmendRequest):
            if request.time_in_force not in capabilities.amend_time_in_force:
                raise NotSupportedError("This amendment time in force is not supported")
            if request.session not in capabilities.amend_sessions:
                raise NotSupportedError("This amendment session is not supported")
        if isinstance(request, OrderRequest):
            # A fractional request gets its own explicit refusal below (FractionalNotSupportedError).
            if not request.fractional and request.order_type not in capabilities.kinds_for(request.market):
                raise NotSupportedError("This order type is not supported")
            if request.time_in_force not in capabilities.time_in_force:
                raise NotSupportedError("This time in force is not supported")
            if request.session not in capabilities.sessions:
                raise NotSupportedError("This order session is not supported")
        if isinstance(request, OrderRequest) and request.fractional:
            # Fractional shares are refused here, before any vendor call, unless the
            # capability table says this broker/market has the API for it. No rounding.
            if request.amount is not None and not capabilities.supports_amount_market_buy:
                raise FractionalNotSupportedError("amount-based buy")
            if request.amount is None and not capabilities.supports_fractional_market_sell:
                raise FractionalNotSupportedError("fractional quantity sell")
        if isinstance(request, OrderRequest) and not request.fractional and request.order_type == "market":
            if not capabilities.supports_market_order:
                raise NotSupportedError("Market orders are not supported")
        if isinstance(request, OrderRequest) and request.order_type == "limit":
            if not capabilities.supports_limit_order:
                raise NotSupportedError("Limit orders are not supported")

    def _compatible_order(self, request: OrderRequest) -> OrderRequest:
        aliases = {"best_limit": "best", "priority_limit": "priority", "mid": "midpoint"}
        if request.order_type in aliases or request.order_type in {"ioc", "fok"}:
            BaseBroker._validate_order(self, request)
            updates = ({"order_type": aliases[request.order_type]} if request.order_type in aliases else
                       {"order_type": "limit", "time_in_force": request.order_type})
            return OrderRequest.model_validate(request.model_dump() | updates)
        return request

    def place_order(self, request: OrderRequest) -> Order:
        # Revalidate even models constructed with model_construct/model_copy.
        request = self._compatible_order(OrderRequest.model_validate(request.model_dump()))
        self._validate_order(request)
        return self.safety.execute(
            self.broker_code, self._account, request, lambda: self._call(lambda: self._place_order(request))
        ).model_copy(update={"client_key": request.client_key})

    def cancel_order(self, request: CancelRequest) -> Order:
        request = CancelRequest.model_validate(request.model_dump())
        self._validate_order(request)
        return self.safety.execute(
            self.broker_code, self._account, request, lambda: self._call(lambda: self._cancel_order(request))
        ).model_copy(update={"client_key": request.client_key})

    def amend_order(
        self, request: AmendRequest, *, order_id: str | None = None, broker_ref: Mapping[str, str] | None = None
    ) -> Order:
        """Amend an open remainder's price in place: the one amend shape for every adapter.

        ``order_id`` (keyword) is only for the pre-unification call shape ``amend_order(OrderRequest,
        order_id=...)``; a plain OrderRequest is lifted into an AmendRequest. A broker without a native
        amend TR reports ``supports_amend=False`` and refuses here before any HTTP: the caller then cancels,
        waits for a confirmed cancel, and places anew (never two live orders at once).

        The guarded mutation shares the *place* idempotency namespace on purpose: a client key used for a
        placement cannot also send an amendment, and a lost amend answer keeps its claim so a retry with
        the same key is blocked until the caller reconciles. Never retried automatically.
        """
        if not isinstance(request, AmendRequest):
            if order_id is None:
                raise ConfigurationError("amend_order needs an AmendRequest (or order_id=)")
            if request.order_type != "limit":
                raise NotSupportedError("Only a limit order can be amended in place")
            if not isinstance(order_id, str) or not order_id:
                raise ConfigurationError("Invalid original order number")
            request = AmendRequest(**{**request.model_dump(exclude={"order_type"}), "order_id": order_id,
                                      "broker_ref": dict(broker_ref or {})})
        elif order_id is not None and order_id != request.order_id:
            raise ConfigurationError("order_id conflicts with the AmendRequest")
        elif broker_ref is not None:
            request = request.model_copy(update={"broker_ref": dict(broker_ref)})
        request = AmendRequest.model_validate(request.model_dump())
        if not self.capabilities().supports_amend:
            raise NotSupportedError("Amend is not supported; cancel, confirm, then place anew")
        self._validate_order(request)
        prepared = self._prepare_amend(request)
        guarded = OrderRequest.model_validate(request.model_dump(exclude={"order_id", "broker_ref"}))
        return self.safety.execute(
            self.broker_code, self._account, guarded, lambda: self._call(lambda: self._amend_order(request, prepared))
        ).model_copy(update={"client_key": request.client_key})

    def _prepare_amend(self, request: AmendRequest) -> Any:
        """Read-only checks before the guarded write; the returned context goes to ``_amend_order``."""
        if request.broker_ref:
            raise ConfigurationError("Unknown broker_ref key for this broker")
        return None

    def _amend_order(self, request: AmendRequest, prepared: Any = None) -> Order:
        raise NotSupportedError("Amend is not implemented")

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
                return candidate.model_copy(update={"client_key": order.client_key})
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
