"""Toss asynchronous client, owned by one persistent loop per adapter."""

import asyncio
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from importlib import import_module
from typing import Any

from pykbroker.exceptions import BrokerRejectedError, ConfigurationError, InvalidResponseError, MissingDependencyError
from pykbroker.models import Balance, CancelRequest, Capabilities, Order, OrderRequest, Position, Quote
from pykbroker.safety.gate import OrderSafety
from pykbroker.safety.idempotency import order_key

from ._common import BaseBroker, decimal, optional_price, whole


class _TossBridge:
    def __init__(
        self,
        *,
        account_seq: int,
        credentials_options: Mapping[str, Any],
        dry_run: bool = True,
        live_trading_enabled: bool = False,
    ) -> None:
        try:
            context_type = import_module("toss_openapi.broker.account_context").AccountContext
            broker_type = import_module("toss_openapi.broker.multi_account").MultiAccountTossBroker
            settings_type = import_module("toss_openapi.config").TossSettings
            credentials_type = import_module("toss_openapi.credentials").TossCredentials
            self._models = import_module("toss_openapi.models.order")
        except ImportError:
            raise MissingDependencyError(
                "Supply toss_openapi source and pykbroker[toss], or inject sdk_factory"
            ) from None
        self._seq, self._dry_run = account_seq, dry_run
        self._broker = broker_type(
            credentials_type(**credentials_options),
            settings_type(dry_run=dry_run, live_trading_enabled=live_trading_enabled),
            accounts=[context_type(account_seq=account_seq, dry_run=dry_run)],
        )

    async def get_positions(self):
        return await self._broker.get_positions(account_seq=self._seq)

    async def get_buying_power(self, currency):
        return await self._broker.get_buying_power(currency, account_seq=self._seq)

    async def get_sellable_quantity(self, symbol):
        return await self._broker.client_for(account_seq=self._seq).order.get_sellable_quantity(symbol)

    async def get_prices(self, symbols):
        return await self._broker.market_data.get_prices(symbols)

    async def get_orderbook(self, symbol):
        return await self._broker.market_data.get_orderbook(symbol)

    async def list_open_orders(self, symbol=None):
        return await self._broker.list_orders(self._models.OrderStatusFilter.OPEN, account_seq=self._seq, symbol=symbol)

    async def submit_order(self, **kwargs):
        kwargs["side"] = self._models.OrderSide(kwargs["side"])
        kwargs["order_type"] = self._models.OrderType(kwargs["order_type"])
        kwargs["time_in_force"] = self._models.TimeInForce(kwargs["time_in_force"])
        result = await self._broker.submit_order(
            self._models.OrderCreateRequest(**kwargs), account_seq=self._seq, dry_run=self._dry_run
        )
        return asdict(result) if is_dataclass(result) else result if isinstance(result, dict) else vars(result)

    async def cancel_order(self, order_id):
        result = await self._broker.cancel_order(order_id, account_seq=self._seq, dry_run=self._dry_run)
        return asdict(result) if is_dataclass(result) else result if isinstance(result, dict) else vars(result)

    async def aclose(self):
        await self._broker.aclose()


class Toss(BaseBroker):
    broker_code = "toss"

    def __init__(
        self,
        *,
        account: str,
        account_seq: int,
        sdk_factory: Callable[..., Any] | None = None,
        credentials_options: Mapping[str, Any] | None = None,
        safety: OrderSafety | None = None,
        market: str = "KR",
        sdk_dry_run: bool = True,
        sdk_live_trading_enabled: bool = False,
        virtual: bool = False,
        halt_provider: Callable[[str], bool] | None = None,
    ) -> None:
        # Toss has no separate mock-trading domain, so `virtual=True` is refused upstream.
        super().__init__(account=account, safety=safety, market=market, virtual=virtual, halt_provider=halt_provider)
        if type(account_seq) is not int or account_seq <= 0:
            raise ConfigurationError("Supply the vendor accountSeq as a positive integer")
        self._factory = sdk_factory or _TossBridge
        self._options = dict(
            account_seq=account_seq,
            credentials_options=dict(credentials_options or {}),
            dry_run=sdk_dry_run,
            live_trading_enabled=sdk_live_trading_enabled,
        )
        self._sdk = None
        self._loop = None
        self._lock = threading.RLock()

    def _run(self, method: str, *args: Any, **kwargs: Any) -> Any:
        # Use asyncio.to_thread for callers already inside an async application.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise ConfigurationError("Use asyncio.to_thread for synchronous Toss methods from an event loop")

        async def invoke():
            if self._sdk is None:
                self._sdk = self._factory(**self._options)
            return await getattr(self._sdk, method)(*args, **kwargs)

        with self._lock:
            if self._closed:
                raise ConfigurationError("Broker is closed")
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
            return self._call(lambda: self._loop.run_until_complete(invoke()))

    def capabilities(self) -> Capabilities:
        # 🚨 B′-0/B′2-1: 거래소 선언(`venues`)은 손으로 다시 적지 않고 base 에서 받아 확장한다.
        # 이 메서드가 base 를 통째로 대체하면서 `venues` 를 빠뜨렸고, A′ 2라운드가
        # `requires_venue` 를 강제로 올린 뒤부터 venue 를 채운 국내 주문이 전부
        # NotSupportedError("Unknown venue…") 로 죽었다(_common.py:_validate_order).
        # 확장은 `model_copy(update=)` 로 하지 않는다 — 그 경로는 검증을 건너뛰므로
        # 모순쌍(requires_venue=True·venues=())이 조용히 살아남는다. 값을 다시 만들어
        # Capabilities 자신의 검증을 반드시 통과시킨다.
        base = super().capabilities()
        update = dict(
            supports_spot=True,
            supports_market_order=self.market == "KR",
            supports_single_order_cancel=True,
            supports_order_history=True,
            realtime_open_orders=True,
            markets=(self.market,),
        )
        return Capabilities(**{**base.model_dump(), **update})

    def get_quote(self, symbol: str, *, market: str = "KR") -> Quote:
        symbol = self._symbol(symbol, market)

        def read():
            prices = self._run("get_prices", [symbol])
            if len(prices) != 1 or prices[0]["symbol"] != symbol:
                raise InvalidResponseError("Toss quote symbol missing or mismatched")
            book = self._run("get_orderbook", symbol)
            return Quote(
                symbol=symbol,
                market=market,
                last=decimal(prices[0]["lastPrice"]),
                bid=optional_price(book["bids"][0]["price"]) if book["bids"] else None,
                ask=optional_price(book["asks"][0]["price"]) if book["asks"] else None,
            )

        return self._call(read)

    def get_balance(self) -> Balance:
        def read():
            currency = "KRW" if self.market == "KR" else "USD"
            positions = self._run("get_positions")
            cash = self._run("get_buying_power", currency)
            return Balance(
                currency=currency,
                cash_available=decimal(cash["cashBuyingPower"]),
                position_value=decimal(positions["marketValue"]["amount"][currency.lower()]),
            )

        return self._call(read)

    def get_positions(self) -> list[Position]:
        def read():
            result = []
            for row in self._run("get_positions")["items"]:
                if row["marketCountry"] != self.market:
                    continue
                sellable = self._run("get_sellable_quantity", row["symbol"])
                result.append(
                    Position(
                        symbol=row["symbol"],
                        name=row["name"],
                        market=self.market,
                        quantity=whole(row["quantity"]),
                        sellable=whole(sellable["sellableQuantity"]),
                        average_price=decimal(row["averagePurchasePrice"]),
                        last=decimal(row["lastPrice"]),
                        market_value=decimal(row["marketValue"]["amount"]),
                    )
                )
            return result

        return self._call(read)

    def list_orders(self, symbol: str | None = None) -> list[Order]:
        if symbol is not None:
            symbol = self._symbol(symbol, self.market)

        def read():
            response = self._run("list_open_orders", symbol)
            if response.get("hasNext"):
                raise InvalidResponseError("Toss order pagination requires an extended SDK bridge")
            states = {
                "PENDING": "open",
                "OPEN": "open",
                "PARTIALLY_FILLED": "partial",
                "FILLED": "filled",
                "CANCELED": "cancelled",
                "CANCELLED": "cancelled",
                "REJECTED": "rejected",
            }
            return [
                Order(
                    id=row["orderId"],
                    symbol=row["symbol"],
                    market=self.market,
                    quantity=whole(row["quantity"]),
                    filled=whole(row["execution"]["filledQuantity"]),
                    status=states.get(row["status"], "unknown"),
                )
                for row in response["orders"]
                if symbol is None or row["symbol"] == symbol
            ]

        return self._call(read)

    def _place_order(self, request: OrderRequest) -> Order:
        if request.order_type == "close" and self.market == "KR":
            raise ConfigurationError("Toss close orders require the US market")
        symbol = self._symbol(request.symbol, request.market)
        client_id = order_key(
            account=self._account,
            broker="toss",
            symbol=symbol,
            side=request.side,
            quantity=request.quantity,
            cycle=request.client_key,
            price=request.price,
            order_type=request.order_type,
        )
        response = self._run(
            "submit_order",
            symbol=symbol,
            side=request.side.upper(),
            order_type="MARKET" if request.order_type == "market" else "LIMIT",
            quantity=str(request.quantity),
            price=format(request.price, "f") if request.price is not None else None,
            time_in_force="CLS" if request.order_type == "close" else "DAY",
            client_order_id=client_id,
        )
        if response.get("submitted") is not True:
            raise BrokerRejectedError("Toss SDK did not confirm submission")
        if not response.get("order_id"):
            raise InvalidResponseError("Toss response lacks an order identifier")
        return Order(
            id=response["order_id"],
            symbol=symbol,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            price=request.price,
            status="accepted",
            market=request.market,
        )

    def _cancel_order(self, request: CancelRequest) -> Order:
        # Vendor endpoint cancels all remaining quantity of one order.
        orders = self.list_orders(request.symbol)
        matching = [order for order in orders if order.id == request.order_id]
        if len(matching) != 1 or matching[0].quantity is None or matching[0].filled is None:
            raise InvalidResponseError("Cannot establish the requested Toss order's remaining quantity")
        if request.quantity != matching[0].quantity - matching[0].filled:
            raise ConfigurationError("Toss cancellation requires the full remaining quantity")
        response = self._run("cancel_order", request.order_id)
        if response.get("status") not in {"CANCELLED", "CANCELED", "CANCEL_SUBMITTED"}:
            raise BrokerRejectedError("Toss cancellation was not acknowledged")
        return Order(id=request.order_id, symbol=request.symbol, status="accepted", market=self.market)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                if self._sdk is not None:
                    self._run("aclose")
            finally:
                if self._loop is not None:
                    self._loop.close()
                super().close()
