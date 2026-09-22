"""Toss asynchronous client, owned by one persistent loop per adapter."""

import asyncio
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from importlib import import_module
from typing import Any
from zoneinfo import ZoneInfo

from pykbroker.exceptions import (
    BrokerRejectedError,
    ConfigurationError,
    DryRunError,
    InvalidResponseError,
    MissingDependencyError,
    NotSupportedError,
    SpecUnverifiedError,
)
from pykbroker.models import Balance, CancelRequest, Capabilities, Order, OrderRequest, Position, Quote
from pykbroker.safety.gate import OrderSafety
from pykbroker.safety.idempotency import order_key
from pykbroker.safety.rate_limit import RateLimiter, credential_rate_limiter

from ._common import BaseBroker, decimal, optional_price, whole

# SDK 호출 이름 → TOKEN_BUCKETS 그룹. 값은 toss_call_rates() 가 SDK 에서 읽는다.
_METHOD_GROUPS = {
    "get_prices": "MARKET_DATA",
    "get_orderbook": "MARKET_DATA",
    "get_positions": "ASSET",
    "get_buying_power": "ORDER_INFO",
    "get_sellable_quantity": "ORDER_INFO",
    "list_open_orders": "ORDER_HISTORY",
    "submit_order": "ORDER",
    "cancel_order": "ORDER",
}

# toss-openapi 가 없을 때의 표. 설치돼 있으면 TOKEN_BUCKETS 가 이 값을 덮는다.
_FALLBACK_TPS = {
    "AUTH": 5.0,
    "ACCOUNT": 1.0,
    "ASSET": 5.0,
    "ORDER": 10.0,
    "ORDER_INFO": 6.0,
    "ORDER_HISTORY": 5.0,
    "MARKET_DATA": 15.0,
}
_KST = ZoneInfo("Asia/Seoul")


def toss_call_rates() -> dict[str, float]:
    """SDK TOKEN_BUCKETS 의 평시 tps. 패키지가 없으면 같은 표의 사본."""
    try:
        buckets = import_module("toss_openapi.transport.retry").TOKEN_BUCKETS
    except ImportError:
        return dict(_FALLBACK_TPS)
    return {name: float(limit.tps) for name, limit in buckets.items()}


def _order_info_peak_tps() -> float:
    try:
        limit = import_module("toss_openapi.transport.retry").TOKEN_BUCKETS["ORDER_INFO"]
    except ImportError:
        return 3.0
    return float(limit.peak_tps if limit.peak_tps is not None else limit.tps)


def _in_order_info_peak(now: datetime) -> bool:
    """ORDER_INFO 피크 창 09:00–09:10 KST. 끝 시각은 포함하지 않는다."""
    local = now.astimezone(_KST)
    start = local.replace(hour=9, minute=0, second=0, microsecond=0)
    end = local.replace(hour=9, minute=10, second=0, microsecond=0)
    return start <= local < end


class _TossBridge:
    """v1.2.17 클라이언트. 계좌 헤더와 TokenStore 는 여기서 SDK 로 넘긴다.

    TokenStore.get/set 은 동기다. TokenManager 가 이 브리지의 이벤트 루프 안에서
    그 메서드를 그대로 호출한다(프로세스 간 락은 store 구현이 잡는다).
    """

    def __init__(
        self,
        *,
        account_seq: int,
        credentials_options: Mapping[str, Any],
        dry_run: bool = True,
        live_trading_enabled: bool = False,
        token_store: Any | None = None,
    ) -> None:
        try:
            context_type = import_module("toss_openapi.broker.account_context").AccountContext
            guard_type = import_module("toss_openapi.broker.order_guard").OrderGuard
            settings_type = import_module("toss_openapi.config").TossSettings
            credentials_type = import_module("toss_openapi.credentials").TossCredentials
            client_type = import_module("toss_openapi.client").TossOpenApiClient
            token_manager_type = import_module("toss_openapi.auth.token_manager").TokenManager
            transport_type = import_module("toss_openapi.transport.http").HttpTransport
            self._models = import_module("toss_openapi.models.order")
        except ImportError:
            raise MissingDependencyError(
                "Supply toss_openapi source and pykbroker[toss], or inject sdk_factory"
            ) from None
        self._dry_run = dry_run
        settings = settings_type(dry_run=dry_run, live_trading_enabled=live_trading_enabled)
        credentials = credentials_type(**credentials_options)
        self._transport = transport_type(settings)
        # store 가 None 이면 SDK 기본 메모리(TokenCache).
        manager = token_manager_type(credentials, self._transport, store=token_store)
        # account_seq 를 클라이언트에 묶으면 계좌 API 에 X-Tossinvest-Account 가 붙는다.
        self._client = client_type(
            credentials,
            settings,
            account_seq=account_seq,
            transport=self._transport,
            token_manager=manager,
        )
        self._context = context_type(account_seq=account_seq, dry_run=dry_run)
        self._guard = guard_type(settings)

    async def get_positions(self):
        return await self._client.account.get_holdings()

    async def get_buying_power(self, currency):
        return await self._client.order.get_buying_power(currency)

    async def get_sellable_quantity(self, symbol):
        return await self._client.order.get_sellable_quantity(symbol)

    async def get_prices(self, symbols):
        return await self._client.market_data.get_prices(symbols)

    async def get_orderbook(self, symbol):
        return await self._client.market_data.get_orderbook(symbol)

    async def list_open_orders(self, symbol=None):
        return await self._client.order.list_orders(self._models.OrderStatusFilter.OPEN, symbol=symbol)

    async def submit_order(self, **kwargs):
        # 드라이런은 HTTP 로 내려가지 않는다. 가드를 지우면 기록 픽스처 테스트가 빨강.
        if self._dry_run:
            return {"submitted": False, "status": "DRY_RUN"}
        kwargs["side"] = self._models.OrderSide(kwargs["side"])
        kwargs["order_type"] = self._models.OrderType(kwargs["order_type"])
        kwargs["time_in_force"] = self._models.TimeInForce(kwargs["time_in_force"])
        order = self._models.OrderCreateRequest(**kwargs)
        self._guard.check_create(order, account=self._context)
        response = await self._client.order.create_order(order)
        return {"submitted": True, "order_id": response.order_id, "status": "SUBMITTED"}

    async def cancel_order(self, order_id):
        if self._dry_run:
            return {"submitted": False, "status": "DRY_RUN", "order_id": order_id}
        self._guard.check_live_allowed(self._context)
        result = await self._client.order.cancel_order(order_id)
        return {"submitted": True, "status": "CANCELLED", "order_id": result.order_id}

    async def aclose(self):
        await self._client.aclose()
        await self._transport.aclose()


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
        token_store: Any | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        # Toss has no separate mock-trading domain, so `virtual=True` is refused upstream.
        super().__init__(account=account, safety=safety, market=market, virtual=virtual, halt_provider=halt_provider)
        # 🚨 B′2-2: 토스 해외주식은 벤더 명세로 확인된 적이 없다 — 거래소 코드도, 주문 유형도,
        # 취소 계약도 모른다. 「markets=("US",) 를 선언하고 실제로는 전부 거절」하는 상태는
        # 결함이 아니라 거짓 선언이다. 확인 전까지는 생성 시점에 명시적으로 잠근다.
        if self.market != "KR":
            raise SpecUnverifiedError("Toss overseas venues are not verified; only KR is supported")
        if type(account_seq) is not int or account_seq <= 0:
            raise ConfigurationError("Supply the vendor accountSeq as a positive integer")
        self._factory = sdk_factory or _TossBridge
        self._options = dict(
            account_seq=account_seq,
            credentials_options=dict(credentials_options or {}),
            dry_run=sdk_dry_run,
            live_trading_enabled=sdk_live_trading_enabled,
            token_store=token_store,
        )
        credential = str((credentials_options or {}).get("api_key") or account)
        if rate_limiter is None:
            rates = {name: toss_call_rates()[name] for name in set(_METHOD_GROUPS.values())}
            rates["ORDER_INFO_PEAK"] = _order_info_peak_tps()
            rate_limiter = credential_rate_limiter("toss", credential, rates)
        self._limiter = rate_limiter
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

        group = _METHOD_GROUPS.get(method)
        if group is not None:
            if group == "ORDER_INFO" and _in_order_info_peak(datetime.now(_KST)):
                group = "ORDER_INFO_PEAK"
            self._limiter.wait(group)

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
            # US 는 생성 시점에 잠겨 있다. 매수가능은 항상 buying-power KRW.
            positions = self._run("get_positions")
            cash = self._run("get_buying_power", "KRW")
            if cash.get("currency") != "KRW":
                raise InvalidResponseError("Toss buying power must be quoted in KRW")
            return Balance(
                currency="KRW",
                cash_available=decimal(cash["cashBuyingPower"]),
                position_value=decimal(positions["marketValue"]["amount"]["krw"]),
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
                "PARTIAL_FILLED": "partial",
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
        # 종가(CLS) 주문은 벤더 해외 세션 전용이고, 그 해외 경로는 B′2-2 로 잠겨 있다.
        if request.order_type == "close":
            raise NotSupportedError("Toss close orders need the unverified overseas session")
        # 기본 드라이런. 이 가드를 지우면 안전 게이트가 열려 있어도 벤더 제출 테스트가 빨강.
        if self._options["dry_run"]:
            raise DryRunError("dry_run: Toss live submission is disabled")
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
