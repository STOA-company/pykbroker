"""DB Securities translation with injected account-bound Pydb client/helpers."""

from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal
from importlib import import_module
from typing import Any

from pykbroker.exceptions import (
    ConfigurationError,
    FractionalNotSupportedError,
    InvalidResponseError,
    MissingDependencyError,
    NotSupportedError,
)
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
        """KR may use the caller's halt provider; US requires documented native evidence."""
        if self.market == "US":
            code = self._symbol(symbol, "US")
            rules = super().get_rules(code, now=now, trading_day=trading_day)
            # No verified US native enum: a caller True may veto, False cannot clear unknown.
            return rules.model_copy(update={"halted": True if rules.halted is True else None})
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
            return Balance(cash_available=decimal(data["Dps2"]), position_value=decimal(data["TotEvalAmt"]))

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
            rows = [
                row
                for page in pages
                for row in check_response(page, "dbfi")["Out1" if self.market == "KR" else "Out"]
            ]
            kr = self.market == "KR"
            children = _kr_children(rows) if kr else {}
            result = []
            for row in rows:
                if kr and _is_kr_cancel_row(row):
                    # A cancel order is evidence about its original order (OrgOrdNo), not an order of
                    # its own: it is folded into the original row below and never listed by itself.
                    continue
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
                if kr and _kr_rest_cancelled(row, children.get(_kr_code(row["OrdNo"]), []),
                                             filled, remaining, quantity):
                    # Filled N (possibly 0) and the venue confirmed the cancel of the rest.
                    status = "cancelled"
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
        if request.fractional:
            # pydbsec buy()/sell() take quantity: int (no fractional order field). Refuse; never int().
            raise FractionalNotSupportedError("DBFI orders are whole shares")
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


# DB증권 OpenAPI 체결/미체결조회 CSPAQ04800 (POST /api/v1/trading/kr-stock/inquiry/transaction-history),
# Out1 fields per the vendor's own spec (DBsecurities/dbsec-open-api
# examples/kr_stock_order/kr_stock_inquire_executions.py; guide openapi.dbsec.co.kr api_id=815bbd65-...):
#   OrgOrdNo 원주문번호 · OrdQty 주문수량 · AllExecQty 전체체결수량 · MrcAbleQty 정정취소가능수량 ·
#   MrcQty 정정취소수량 · OrdTrxPtnCode 주문처리유형코드 0:정상 6:정정확인 8:취소확인 7:정정중 9:취소중.
# The cancel TR CSPAT00800 numbers the cancel as its own order (Out.OrdNo) with PrntOrdNo = the original.
_KR_CANCEL_CONFIRMED = "8"
_KR_CANCEL_ROW_CODES = frozenset({"8", "9"})
_KR_IN_FLIGHT = frozenset({"6", "7", "9"})


def _kr_code(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return str(int(text)) if text.isdecimal() else text


def _kr_parent(row: dict) -> str | None:
    parent = _kr_code(row.get("OrgOrdNo"))
    if parent in (None, "0") or parent == _kr_code(row.get("OrdNo")):
        return None
    return parent


def _is_kr_cancel_row(row: dict) -> bool:
    return _kr_parent(row) is not None and _kr_code(row.get("OrdTrxPtnCode")) in _KR_CANCEL_ROW_CODES


def _kr_children(rows: list) -> dict[str, list]:
    out: dict[str, list] = {}
    for row in rows:
        parent = _kr_parent(row)
        if parent is not None:
            out.setdefault(parent, []).append(row)
    return out


def _kr_rest_cancelled(row: dict, children: list, filled: int, remaining: int, quantity: int | None) -> bool:
    """True only when CSPAQ04800 itself proves the unfilled rest was cancelled; anything else is not proof.

    Needs: nothing left to cancel (MrcAbleQty 0), a 취소확인 (code 8) on the order or on a cancel row linked by
    OrgOrdNo, no 정정·취소 still in flight (6/7/9) or unclassified linked row, and the cancelled quantity the venue
    reports (MrcQty on the order and/or the confirmed cancel rows' OrdQty) equal to OrdQty - AllExecQty.
    """
    if quantity is None or remaining != 0 or filled >= quantity:
        return False
    own = _kr_code(row.get("OrdTrxPtnCode"))
    kid_codes = [_kr_code(kid.get("OrdTrxPtnCode")) for kid in children]
    if own in _KR_IN_FLIGHT or any(c != _KR_CANCEL_CONFIRMED for c in kid_codes):
        return False
    if own != _KR_CANCEL_CONFIRMED and not children:
        return False
    rest = quantity - filled
    reported = []
    if row.get("MrcQty") not in (None, "") and whole(row["MrcQty"]) > 0:
        reported.append(whole(row["MrcQty"]))
    if children:
        if any(kid.get("OrdQty") in (None, "") for kid in children):
            return False
        reported.append(sum(whole(kid["OrdQty"]) for kid in children))
    return bool(reported) and all(q == rest for q in reported)
