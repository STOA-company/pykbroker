"""DB Securities translation with injected account-bound Pydb client/helpers."""

from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal
from importlib import import_module
from typing import Any

from pykbroker.exceptions import ConfigurationError, InvalidResponseError, MissingDependencyError, NotSupportedError
from pykbroker.models import Balance, CancelRequest, Capabilities, Order, OrderRequest, Position, Quote, Rules
from pykbroker.rules.kr import equity_rules
from pykbroker.safety.gate import OrderSafety

from ._common import BaseBroker, check_response, decimal, optional_price, whole

US_VENUES = ("NYSE", "NASDAQ", "AMEX")


class DBFI(BaseBroker):
    broker_code = "dbfi"

    def __init__(
        self,
        *,
        account: str,
        client: Any = None,
        api: Any = None,
        client_factory: Callable[..., Any] | None = None,
        client_options: Mapping[str, Any] | None = None,
        safety: OrderSafety | None = None,
        market: str = "KR",
        venue: str | None = None,
        cash_provider: Callable[[Any], Decimal] | None = None,
        is_integrated: bool = False,
        virtual: bool = False,
        halt_provider: Callable[[str], bool] | None = None,
    ) -> None:
        super().__init__(account=account, safety=safety, market=market, virtual=virtual, halt_provider=halt_provider)
        if market == "US" and venue not in US_VENUES:
            raise ConfigurationError("US DBFI requires NYSE, NASDAQ, or AMEX venue")
        if not isinstance(is_integrated, bool):
            raise ConfigurationError("is_integrated must be an explicit boolean")
        self.is_integrated = is_integrated
        self.venue, self._cash_provider = venue, cash_provider
        self.region = "domestic" if market == "KR" else "overseas"
        self._client, self._api = client, api
        self._factory, self._options = client_factory, dict(client_options or {})
        self._owns_client = client is None

    def _connect(self) -> tuple[Any, Any]:
        try:
            if self._client is None:
                factory = self._factory or import_module("Pydb.pydbfi").DBFI
                if not self._options and self._factory is None:
                    raise ConfigurationError("Supply DBFI client_options or an authenticated client")
                self._client = factory(**self._options)
        except ImportError:
            raise MissingDependencyError("Supply Pydb source and pykbroker[dbfi], or inject client and api") from None
        return self._client, self._api

    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_spot=True,
            # DB overseas has no market order either; US is limit-only on both brokers.
            supports_market_order=self.market == "KR",
            supports_single_order_cancel=True,
            supports_nxt=self.market == "KR",
            supports_order_history=True,
            realtime_open_orders=True,
            requires_venue=self.market == "US",
            venues=US_VENUES if self.market == "US" else ("KRX", "NXT"),
            markets=(self.market,),
        )

    def get_rules(
        self,
        symbol: str,
        *,
        now: datetime | None = None,
        trading_day: Callable[[date], bool] | None = None,
    ) -> Rules:
        """Same contract as KIS. The DB price payload carries no prev close and no verified
        halt field, so the band and the halt stay unknown unless a halt_provider answers."""
        if self.market != "KR":
            return super().get_rules(symbol, now=now, trading_day=trading_day)
        code = self._symbol(symbol, "KR")

        def read() -> Rules:
            client, _ = self._connect()
            price = check_response(client.get_stock_price(region=self.region, stock_code=code, market_code="J"), "dbfi")
            return equity_rules(
                code,
                price=decimal(price["Out"]["Prpr"]),
                halted=self._halt_from_provider(code),
                now=now,
                trading_day=trading_day,
            )

        return self._call(read)

    def get_quote(self, symbol: str, *, market: str = "KR") -> Quote:
        symbol = self._symbol(symbol, market)

        def read() -> Quote:
            client, _ = self._connect()
            kwargs = dict(
                region=self.region,
                stock_code=symbol,
                market_code="J" if self.market == "KR" else {"NYSE": "FY", "NASDAQ": "FN", "AMEX": "FA"}[self.venue],
            )
            price = check_response(client.get_stock_price(**kwargs), "dbfi")["Out"]
            book = check_response(client.get_order_book(**kwargs), "dbfi")["Out"]
            return Quote(
                symbol=symbol,
                market=self.market,
                last=decimal(price["Prpr"]),
                bid=optional_price(book["Bidp1"]),
                ask=optional_price(book["Askp1"]),
            )

        return self._call(read)

    def _balance_pages(self):
        client, _ = self._connect()
        response = client.get_stock_balance(region=self.region)
        pages = response if isinstance(response, list) else [response]
        if not pages:
            raise InvalidResponseError("DBFI balance page missing")
        return [check_response(page, "dbfi") for page in pages]

    def available_cash(self, *, symbol: str | None = None, price: Decimal | None = None) -> Decimal:
        """Broker buying power, never deposits/equity or a locally converted FX amount.

        Pass the intended symbol/price for a pre-order check. The no-argument USD
        snapshot reuses Pydb.services.get_balance_overseas's AAPL/1 query.
        """
        code = self._symbol(symbol or ("005930" if self.market == "KR" else "AAPL"), self.market)
        order_price = decimal(price) if price is not None else Decimal(0 if self.market == "KR" else 1)
        if self.market == "KR" and order_price != order_price.to_integral_value():
            raise ConfigurationError("Domestic DBFI price must be whole won")

        def read() -> Decimal:
            client, _ = self._connect()
            if self._cash_provider is not None:
                return decimal(self._cash_provider(client))
            kwargs = dict(region=self.region, stock_code=code, order_type="2",
                          price=int(order_price) if self.market == "KR" else float(order_price))
            if self.market == "US":
                kwargs["won_fcurr_type"] = "2"  # Native currency, not KRW converted values.
            data = check_response(client.get_able_order_quantity(**kwargs), "dbfi")
            if data.get("rsp_cd") != "00000":
                raise InvalidResponseError("DBFI buying power lacks a success status")
            output = data["Out"]
            if self.market == "KR":
                # CSPBQ00100: 100% margin amount excludes a credit/debit buying-power boost.
                return decimal(output["MgnRat100pctOrdAbleAmt"])
            # CAZCQ01300: Amt0 is integrated margin, Amt1 includes debit and is never used.
            if self.is_integrated and output.get("CsldtMgnUseYn") != "Y":
                raise InvalidResponseError("DBFI integrated margin is not confirmed by the broker")
            key = "AstkOrdAbleAmt0" if self.is_integrated else "AstkOrdAbleAmt"
            return decimal(output[key])

        return self._call(read)

    def get_balance(self) -> Balance:
        def read() -> Balance:
            if self.market == "US":
                client, _ = self._connect()
                if self._cash_provider is not None:
                    cash = decimal(self._cash_provider(client))
                else:
                    usd = [row for page in self._balance_pages() for row in page["Out1"]
                           if row.get("CrcyCode") == "USD"]
                    if len(usd) != 1:
                        raise InvalidResponseError("DBFI native cash requires one USD row")
                    cash = min(decimal(usd[0]["FcurrDps"]), decimal(usd[0]["AstkMnyoutAbleAmt"]))
                return Balance(currency="USD", cash_available=cash,
                               position_value=sum((p.market_value for p in self.get_positions()), Decimal(0)))
            data = self._balance_pages()[0]["Out"]
            return Balance(cash_available=self.available_cash(), position_value=decimal(data["TotEvalAmt"]))

        return self._call(read)

    def get_positions(self) -> list[Position]:
        def read() -> list[Position]:
            result = []
            for page in self._balance_pages():
                if self.market == "US":
                    result.extend(
                        Position(
                            symbol=row["SymCode"],
                            name=row["AstkHanglIsuNm"],
                            market="US",
                            quantity=whole(row["AstkExecBaseQty"]),
                            sellable=whole(row["AstkOrdAbleQty"]),
                            average_price=decimal(row["AstkAvrPchsPrc"]),
                            last=decimal(row["AstkNowPrc"]),
                            market_value=decimal(row["AstkEvalAmt"]),
                        )
                        for row in page["Out2"]
                    )
                else:
                    for row in page["Out1"]:
                        quantity = whole(row["BalQty0"])
                        if quantity == 0:
                            continue
                        result.append(
                            Position(
                                symbol=self._symbol(row["IsuNo"], "KR"),
                                name=row["IsuNm"],
                                quantity=quantity,
                                sellable=whole(row["AbleQty"]),
                                average_price=decimal(row["PchsAmt"]) / quantity,
                                last=decimal(row["NowPrc"]),
                                market_value=decimal(row["EvalAmt"]),
                            )
                        )
            return result

        return self._call(read)

    def list_orders(self, symbol: str | None = None) -> list[Order]:
        if symbol is not None:
            symbol = self._symbol(symbol, self.market)

        def read() -> list[Order]:
            client, _ = self._connect()
            kwargs = dict(region=self.region, execution_status="0", order_type="0")
            if self.market == "US":
                kwargs.update(stock_code=symbol or "", query_type="0", won_fcurr_type="2")
            response = client.get_transaction_history(**kwargs)
            pages = response if isinstance(response, list) else [response]
            result = []
            for page in pages:
                for row in check_response(page, "dbfi")["Out1" if self.market == "KR" else "Out"]:
                    code = self._symbol(row["IsuNo"] if self.market == "KR" else row["SymCode"], self.market)
                    if symbol is not None and code != symbol:
                        continue
                    filled = whole(row["AllExecQty"] if self.market == "KR" else row["AstkExecQty"])
                    remaining = whole(row["MrcAbleQty"] if self.market == "KR" else row["AstkOrdRmqty"])
                    quantity_key = "OrdQty" if self.market == "KR" else "AstkOrdQty"
                    quantity = whole(row[quantity_key]) if quantity_key in row else None
                    if quantity is not None and (filled > quantity or remaining > quantity - filled):
                        raise InvalidResponseError("DBFI execution quantities exceed the order")
                    # Zero cancellable quantity alone never proves completion.
                    status = "filled" if filled and quantity == filled and remaining == 0 else (
                        "partial" if filled and remaining else "open" if remaining else "unknown")
                    fill_price = None
                    if filled:
                        if self.market == "KR":
                            fill_price = optional_price(row.get("AvrExecPrc"))
                        elif row.get("AstkExecAmt") not in (None, ""):
                            if row.get("CrcyCode") != "USD":
                                raise InvalidResponseError("DBFI US execution currency is not USD")
                            # CAZCQ00100 aggregate mode: native execution amount / executed shares.
                            fill_price = decimal(row["AstkExecAmt"]) / filled
                        if fill_price is not None and fill_price <= 0:
                            fill_price = None
                    result.append(
                        Order(
                            id=str(row["OrdNo"]),
                            symbol=code,
                            quantity=quantity,
                            fill_price=fill_price,
                            filled=filled,
                            remaining=remaining,
                            status=status,
                            market=self.market,
                        )
                    )
            return result

        return self._call(read)

    def _place_order(self, request: OrderRequest) -> Order:
        client, _ = self._connect()
        symbol = self._symbol(request.symbol, request.market)
        submit = client.buy if request.side == "buy" else client.sell
        if self.market == "US" and request.order_type != "limit":
            raise NotSupportedError("US DBFI only implements limit orders")
        if self.market == "US" and request.venue not in (None, self.venue):
            raise NotSupportedError("Order venue must match the configured US venue")
        kwargs = dict(
            region=self.region,
            stock_code=symbol,
            quantity=request.quantity,
            price=int(request.price or 0) if self.market == "KR" else float(request.price),
            price_type={"limit": "00", "market": "03", "close": "81"}[request.order_type]
            if self.market == "KR"
            else "1",
        )
        if self.market == "KR":
            kwargs["use_nxt"] = request.venue == "NXT"
        response = submit(**kwargs)
        data = check_response(response, "dbfi")
        if data.get("rsp_cd") != "00000":
            raise InvalidResponseError("DBFI submission response lacks a success status")
        output = data.get("Out", data)
        order_id = output.get("OrdNo", output.get("order_no"))
        if order_id is None or not str(order_id).strip():
            raise InvalidResponseError("DBFI response lacks an order identifier; reconcile before retrying")
        return Order(
            id=str(order_id),
            symbol=symbol,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            price=request.price,
            status="accepted",
            market=self.market,
        )

    def _cancel_order(self, request: CancelRequest) -> Order:
        client, _ = self._connect()
        if not request.order_id.isdecimal():
            raise ConfigurationError("DBFI requires a numeric order identifier")
        kwargs = dict(
            region=self.region,
            order_no=int(request.order_id),
            stock_code=self._symbol(request.symbol, self.market),
            quantity=request.quantity,
        )
        if self.market == "KR":
            kwargs["use_nxt"] = request.venue == "NXT"
        response = client.cancel(**kwargs)
        data = check_response(response, "dbfi")
        if data.get("rsp_cd") != "00000":
            raise InvalidResponseError("DBFI cancellation response lacks a success status")
        # A successful cancel acknowledgement does not establish final fills.
        return Order(id=request.order_id, symbol=request.symbol, status="accepted", market=self.market)

    def close(self) -> None:
        # Pydb's DBFI.close() revokes the access token (api.py:31-32 -> oauth.py:177). That token sits
        # on the account row and the legacy DbfiOrder/TriggerTrader reuse it, so closing this broker
        # ends the session only: the client is dropped, never closed, and nothing reaches the venue.
        if self._owns_client:
            self._client = None
        super().close()
