"""Toss asynchronous client, owned by one persistent loop per adapter."""

import asyncio
import os
import re
import threading
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal
from importlib import import_module
from typing import Any
from zoneinfo import ZoneInfo

from pykbroker.exceptions import (
    BrokerRejectedError,
    ConfigurationError,
    DryRunError,
    FractionalDisabledError,
    FractionalOutsideSessionError,
    InvalidResponseError,
    MissingDependencyError,
    NotSupportedError,
    SpecUnverifiedError,
    TossRejectedError,
    TransportError,
)
from pykbroker.models import Balance, CancelRequest, Capabilities, Order, OrderRequest, Position, Quote, Rules
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
    "get_exchange_rate": "MARKET_INFO",
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
    "MARKET_INFO": 3.0,
}
_KST = ZoneInfo("Asia/Seoul")
_ET = ZoneInfo("America/New_York")


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


FRACTIONAL_MARKET_ALLOWED_ENV = "FRACTIONAL_MARKET_ALLOWED"
_OFF_VALUES = frozenset({"0", "false", "off", "no"})


def fractional_market_allowed(env: Mapping[str, str] | None = None) -> bool:
    """D205 (2026-10-16): Toss US fractional (amount MARKET buy / decimal MARKET sell) is ON by default.

    The same flag name is read by the platform and the trader. Only an explicit "0"/"false"/"off"/"no"
    switches it off; an unset or unparsable value keeps the default ON. Whole-share orders never look here.
    """
    raw = (os.environ if env is None else env).get(FRACTIONAL_MARKET_ALLOWED_ENV)
    if raw is None:
        return True
    return str(raw).strip().lower() not in _OFF_VALUES


def in_fractional_session(now: datetime) -> bool:
    """Toss OpenAPI 1.2.24: amount / fractional-quantity orders are accepted from the US regular
    session open until one hour before its close (ET 09:30-15:00, weekdays). The vendor answers
    422 `amount-order-outside-regular-hours` / `fractional-quantity-outside-regular-hours` outside
    it; this check refuses before the HTTP so a closed window never spends a token or an
    idempotency key. Exchange holidays are not modelled here (the vendor still refuses them).
    """
    if now.tzinfo is None:
        raise ConfigurationError("Session clock must include a timezone")
    local = now.astimezone(_ET)
    if local.weekday() >= 5:
        return False
    start = local.replace(hour=9, minute=30, second=0, microsecond=0)
    end = local.replace(hour=15, minute=0, second=0, microsecond=0)
    return start <= local < end


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
        base_url: str | None = None,
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
            self._api_error_type = import_module("toss_openapi.errors").TossApiError
        except ImportError:
            raise MissingDependencyError(
                "Supply toss_openapi source and pykbroker[toss], or inject sdk_factory"
            ) from None
        self._dry_run = dry_run
        # base_url None = SDK default (operations, unchanged call). The caller owns
        # any override policy; the SDK derives the token URL from this base.
        if base_url is None:
            settings = settings_type(dry_run=dry_run, live_trading_enabled=live_trading_enabled)
        else:
            settings = settings_type(
                base_url=base_url, dry_run=dry_run, live_trading_enabled=live_trading_enabled
            )
        credentials = credentials_type(**credentials_options)
        self._transport = transport_type(settings)
        # store 가 None 이면 SDK 기본 메모리(TokenCache).
        manager = token_manager_type(credentials, self._transport, store=token_store)
        self._manager = manager
        self._account_seq = account_seq
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

    async def get_exchange_rate(self, base_currency, quote_currency):
        return await self._client.market_info.get_exchange_rate(base_currency, quote_currency)

    async def get_sellable_quantity(self, symbol):
        return await self._client.order.get_sellable_quantity(symbol)

    async def get_prices(self, symbols):
        return await self._client.market_data.get_prices(symbols)

    async def get_orderbook(self, symbol):
        return await self._client.market_data.get_orderbook(symbol)

    async def list_open_orders(self, symbol=None):
        return await self._client.order.list_orders(self._models.OrderStatusFilter.OPEN, symbol=symbol)

    async def _post_once(self, path: str, *, json_body=None):
        # OrderClient._call refreshes a revoked token and replays even when
        # retryable=False. Use the SDK transport directly for one-shot writes.
        token = await self._manager.get_access_token()
        return await self._transport.request(
            "POST", path, json_body=json_body, token=token,
            account_seq=self._account_seq, retryable=False,
        )

    def _raise_write_error(self, exc):
        if (exc.status_code == 408 or (exc.status_code == 409 and exc.code == "request-in-progress")
                or exc.code == "idempotency-key-conflict"):
            raise TransportError("Toss write outcome is uncertain; reconcile before another order") from None
        if 400 <= exc.status_code < 500:
            raw_code = exc.code
            code = (
                raw_code
                if isinstance(raw_code, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", raw_code)
                else None
            )
            raise TossRejectedError(code) from None
        raise TransportError("Toss write outcome is uncertain; reconcile before another order") from None

    async def submit_order(self, **kwargs):
        # 드라이런은 HTTP 로 내려가지 않는다. 가드를 지우면 기록 픽스처 테스트가 빨강.
        if self._dry_run:
            return {"submitted": False, "status": "DRY_RUN"}
        kwargs["side"] = self._models.OrderSide(kwargs["side"])
        kwargs["order_type"] = self._models.OrderType(kwargs["order_type"])
        # Amount-based (orderAmount) orders carry no timeInForce (spec oneOf); the SDK refuses one.
        if kwargs.get("time_in_force") is not None:
            kwargs["time_in_force"] = self._models.TimeInForce(kwargs["time_in_force"])
        order = self._models.OrderCreateRequest(**kwargs)
        self._guard.check_create(order, account=self._context)
        try:
            payload = await self._post_once("/api/v1/orders", json_body=order.to_payload())
        except self._api_error_type as exc:
            self._raise_write_error(exc)
        response = self._models.OrderResponse.from_payload(payload)
        return {"submitted": True, "order_id": response.order_id, "status": "SUBMITTED"}

    async def cancel_order(self, order_id):
        if self._dry_run:
            return {"submitted": False, "status": "DRY_RUN", "order_id": order_id}
        self._guard.check_live_allowed(self._context)
        try:
            payload = await self._post_once(f"/api/v1/orders/{order_id}/cancel")
        except self._api_error_type as exc:
            self._raise_write_error(exc)
        result = self._models.OrderOperationResponse.from_payload(payload)
        return {"submitted": True, "status": "CANCEL_SUBMITTED", "order_id": result.order_id}

    async def aclose(self):
        await self._client.aclose()
        await self._transport.aclose()


class Toss(BaseBroker):
    broker_code = "toss"

    def _symbol(self, symbol: str, market: str) -> str:
        code = super()._symbol(symbol, market)
        if market == "US" and re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", code) is None:
            raise NotSupportedError("Toss US pilot requires a US equity ticker")
        return code

    def __init__(
        self,
        *,
        account: str,
        account_seq: int,
        sdk_factory: Callable[..., Any] | None = None,
        credentials_options: Mapping[str, Any] | None = None,
        safety: OrderSafety | None = None,
        market: str = "KR",
        allow_us_pilot: bool = False,
        sdk_dry_run: bool = True,
        sdk_live_trading_enabled: bool = False,
        virtual: bool = False,
        halt_provider: Callable[[str], bool] | None = None,
        token_store: Any | None = None,
        rate_limiter: RateLimiter | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        # Toss has no separate mock-trading domain, so `virtual=True` is refused upstream.
        super().__init__(account=account, safety=safety, market=market, virtual=virtual, halt_provider=halt_provider)
        # SDK v1.2.17 routes US symbols without a venue field. Keep the older
        # construction lock unless the caller explicitly opts into the pilot.
        if type(allow_us_pilot) is not bool:
            raise ConfigurationError("allow_us_pilot must be a boolean")
        self._allow_us_pilot = allow_us_pilot and self.market == "US"
        if self.market == "US" and not self._allow_us_pilot:
            raise SpecUnverifiedError("Toss US is unverified without explicit pilot opt-in")
        if type(account_seq) is not int or account_seq <= 0:
            raise ConfigurationError("Supply the vendor accountSeq as a positive integer")
        self._factory = sdk_factory or _TossBridge
        self._clock = clock or (lambda: datetime.now(_KST))
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
        if self.market == "US":
            if not self._allow_us_pilot:
                raise SpecUnverifiedError("Toss US is unverified without explicit pilot opt-in")
            # Toss order requests identify a US equity by symbol only. Requiring
            # a venue here would reject every valid SDK request before submission.
            return Capabilities(
                supports_spot=True,
                supports_limit_order=True,
                # Toss OpenAPI 1.2.24: US fractional shares — `orderAmount` MARKET BUY and
                # decimal `quantity` MARKET SELL (<=6 dp), regular session only. Whole-share
                # MARKET stays closed (supports_market_order=False, D11 limit-only pilot).
                supports_amount_market_buy=True,
                supports_fractional_market_sell=True,
                supports_single_order_cancel=True,
                supports_order_history=True,
                realtime_open_orders=True,
                markets=("US",),
            )
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

    def get_rules(
        self, symbol: str, *, now: datetime | None = None, trading_day: Callable[[date], bool] | None = None
    ) -> Rules:
        if self.market == "US":
            symbol = self._symbol(symbol, "US")
        rules = super().get_rules(symbol, now=now, trading_day=trading_day)
        if self.market == "US":
            # ACTIVE is listing status; domestic suspension flags cannot clear a US silence.
            return rules.model_copy(update={"halted": True if rules.halted is True else None})
        return rules

    def get_quote(self, symbol: str, *, market: str = "KR") -> Quote:
        symbol = self._symbol(symbol, market)

        def read():
            prices = self._run("get_prices", [symbol])
            if len(prices) != 1 or prices[0]["symbol"] != symbol:
                raise InvalidResponseError("Toss quote symbol missing or mismatched")
            if self.market == "US":
                if prices[0].get("currency") != "USD":
                    raise InvalidResponseError("Toss US quote must be quoted in USD")
            book = self._run("get_orderbook", symbol)
            if self.market == "US" and book.get("currency") != "USD":
                raise InvalidResponseError("Toss US orderbook must be quoted in USD")
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
            currency = "USD" if self.market == "US" else "KRW"
            positions = self._run("get_positions")
            cash = self._run("get_buying_power", currency)
            if cash.get("currency") != currency:
                raise InvalidResponseError(f"Toss buying power must be quoted in {currency}")
            value = positions["marketValue"]["amount"][currency.lower()]
            if self.market == "US" and value is None and not any(
                row.get("marketCountry") == "US" for row in positions["items"]
            ):
                value = "0"
            return Balance(
                currency=currency,
                cash_available=decimal(cash["cashBuyingPower"]),
                position_value=decimal(value),
            )

        return self._call(read)

    def funding_snapshot(self) -> dict[str, Decimal]:
        """Read US and KRW buying power plus the vendor's USD/KRW rate."""
        if self.market != "US" or not self._allow_us_pilot:
            raise NotSupportedError("Toss US funding requires pilot opt-in")

        def read():
            usd = self._run("get_buying_power", "USD")
            krw = self._run("get_buying_power", "KRW")
            rate = self._run("get_exchange_rate", "USD", "KRW")
            if usd.get("currency") != "USD" or krw.get("currency") != "KRW":
                raise InvalidResponseError("Toss buying-power currency mismatch")
            if rate.get("baseCurrency") != "USD" or rate.get("quoteCurrency") != "KRW":
                raise InvalidResponseError("Toss exchange-rate currency mismatch")
            fx = decimal(rate["rate"])
            if fx <= 0:
                raise InvalidResponseError("Toss USD/KRW rate must be positive")
            return {
                "usd_buying_power": decimal(usd["cashBuyingPower"]),
                "krw_buying_power": decimal(krw["cashBuyingPower"]),
                "usd_krw_rate": fx,
            }

        return self._call(read)

    def _holding_groups(self, items: list[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
        """This market's holdings rows by symbol — a row repeated verbatim counts once.

        Toss documents holdings as one entry per held symbol (Asset «종목별 보유 수량», HoldingsItem has no
        account/lot/type field) and the sellable quantity per symbol only (/api/v1/sellable-quantity). The
        documented identity of a row is (symbol, marketCountry, currency). Review 527c P2-T / 535 P1:
        - a row repeated verbatim (every field equal) is one holding listed twice → kept once;
        - rows of one identity that differ in any field (quantity, price, average cost, ...) are kept as
          separate rows like another currency: the symbol's one vendor sellable answer is what the sell
          check uses, the rows only bound it (`_sellable_rows`: answer above the rows' sum → InvalidResponseError);
        - the same symbol in both markets cannot be split, because the sellable read takes the symbol
          only → InvalidResponseError (trader: holdings_unknown, D814);
        - distinct identities of one symbol (another currency) are kept as separate rows and share the
          symbol's one sellable answer (`_sellable_rows`), never one full answer per row.
        Never de-duplicated or merged on quantity.
        """
        groups: dict[str, list[Mapping[str, Any]]] = {}
        seen: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
        other_market: set[str] = set()
        for row in items:
            if row["marketCountry"] != self.market:
                other_market.add(row["symbol"])
                continue
            key = (row["symbol"], row["marketCountry"], row.get("currency"))
            if any(dict(prev) == dict(row) for prev in seen.get(key, ())):
                continue
            seen.setdefault(key, []).append(row)
            groups.setdefault(row["symbol"], []).append(row)
        if other_market & groups.keys():
            raise InvalidResponseError("Toss holdings list one symbol in two markets")
        return groups

    def _sellable_rows(self, symbol: str, rows: list[Mapping[str, Any]]) -> list[tuple[Mapping[str, Any], Decimal, Decimal]]:
        """[(row, exact quantity, exact sellable)] — the symbol's sellable answer is read once and spread over
        its distinct rows in order (each row at most its own quantity), so the rows sum to the vendor answer."""
        answer = self._run("get_sellable_quantity", symbol)["sellableQuantity"]
        if self.market == "US":
            remaining = decimal(answer)
            quantities = [decimal(row["quantity"]) for row in rows]
        else:
            # 국내(KR)는 스펙상 정수라 소수 수량은 계속 오류로 둔다(스펙 위반 응답을 숨기지 않음).
            remaining = Decimal(whole(answer))
            quantities = [Decimal(whole(row["quantity"])) for row in rows]
        if remaining < 0 or remaining > sum(quantities, Decimal(0)):
            raise InvalidResponseError("Toss sellable quantity exceeds the holding")
        result = []
        for row, quantity in zip(rows, quantities):
            part = min(quantity, remaining)
            remaining -= part
            result.append((row, quantity, part))
        return result

    def sellable_quantity(self, symbol: str) -> Decimal:
        """Vendor sellable quantity of one holding (0 when not held), for pre-trade sell checks.

        Exact (US fractional, e.g. 2.57). Read from the per-symbol sellable answer once — never summed
        per holdings row. A self-contradicting holdings answer raises (see `_holding_groups`).
        """
        code = self._symbol(symbol, self.market)

        def read():
            rows = self._holding_groups(self._run("get_positions")["items"]).get(code)
            if not rows:
                return Decimal(0)
            return sum((part for _, _, part in self._sellable_rows(code, rows)), Decimal(0))

        return self._call(read)

    def get_positions(self) -> list[Position]:
        def read():
            result = []
            for symbol, rows in self._holding_groups(self._run("get_positions")["items"]).items():
                for row, exact_qty, exact_sellable in self._sellable_rows(symbol, rows):
                    # 토스 미국 소수점 주식(예: "0.3"): 정확한 수량은 *_exact 에 두고 quantity/sellable 은
                    # 내림한 정수 주식으로 보고한다(벤더는 소수 매도를 MARKET 만 받고, 정수 주문 경로는 잔량을 팔지 않는다).
                    whole_qty = int(exact_qty.to_integral_value(rounding=ROUND_FLOOR))
                    whole_sellable = int(exact_sellable.to_integral_value(rounding=ROUND_FLOOR))
                    us = self.market == "US"
                    result.append(
                        Position(
                            symbol=row["symbol"],
                            name=row["name"],
                            market=self.market,
                            quantity=whole_qty,
                            sellable=whole_sellable,
                            quantity_exact=exact_qty if us else None,
                            sellable_exact=exact_sellable if us else None,
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
        # US 파일럿은 지정가만(D11: 매수·매도). 시장가·종가는 계속 거부하고,
        # 매도 가격 판정(현재가 밴드)은 trader 게이트가 한다.
        if request.fractional:
            # 소수점(금액 매수·소수 수량 매도)은 토스 미국 정규장 창에서만 — HTTP 전에 거부.
            # 모델 검증이 US·MARKET·BUY(amount)/SELL(decimal) 을 이미 강제한다.
            if self.market != "US":
                raise NotSupportedError("Toss fractional orders are US only")
            if not fractional_market_allowed():
                raise FractionalDisabledError()
            if not in_fractional_session(self._clock()):
                raise FractionalOutsideSessionError()
        elif self.market == "US" and request.order_type != "limit":
            raise NotSupportedError("Toss US pilot accepts LIMIT orders only")
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
            # Amount buys hash the USD amount in the quantity slot (quantity is None); client_key keeps
            # the key unique per intent, and order_type/price distinguish it from a share order.
            quantity=request.quantity if request.quantity is not None else request.amount,
            cycle=request.client_key,
            price=request.price,
            order_type=request.order_type,
        )
        if request.amount is not None:
            # OrderCreateAmountBased: orderAmount (USD string), no quantity/price/timeInForce.
            submission = dict(order_amount=format(request.amount, "f"), quantity=None,
                              price=None, time_in_force=None)
        else:
            submission = dict(
                # Decimal quantities are sent verbatim (<=6 dp, validated); never int().
                quantity=format(request.quantity, "f") if isinstance(request.quantity, Decimal)
                else str(request.quantity),
                price=format(request.price, "f") if request.price is not None else None,
                time_in_force="CLS" if request.order_type == "close" else "DAY",
            )
        response = self._run(
            "submit_order",
            symbol=symbol,
            side=request.side.upper(),
            order_type="MARKET" if request.order_type == "market" else "LIMIT",
            client_order_id=client_id,
            **submission,
        )
        if response.get("submitted") is not True:
            raise BrokerRejectedError("Toss SDK did not confirm submission")
        if not response.get("order_id"):
            raise InvalidResponseError("Toss response lacks an order identifier")
        exact = request.quantity if isinstance(request.quantity, Decimal) else None
        return Order(
            id=response["order_id"],
            symbol=symbol,
            side=request.side,
            order_type=request.order_type,
            # Fractional sells: no whole-share count (never floored or rounded) — the exact Decimal
            # is quantity_exact; amount buys: quantity unknown until fills, the USD amount in `amount`.
            quantity=(None if exact is not None else request.quantity),
            quantity_exact=exact,
            amount=request.amount,
            price=request.price,
            status="accepted",
            market=request.market,
        )

    def _cancel_order(self, request: CancelRequest) -> Order:
        if self._options["dry_run"]:
            raise DryRunError("dry_run: Toss live cancellation is disabled")
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
