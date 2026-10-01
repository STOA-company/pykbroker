"""LS KR adapter with an injected REST boundary and a default-closed write lock.

UNVERIFIED (명세 미확인): detailed official guides were inaccessible on 2026-10-01.
Paths/TRs/request blocks reuse experimental/ls.py and trader traders/ls_order.py.
Enum values, rsp_cd meanings, continuation, and t0425 qty/price/cheqty/cheprice
semantics and account-mode binding require official specification comparison.
No default client, credentials, rate budget, or environment unlock is provided.
"""

from pykbroker.brokers._common import decimal, optional_price, whole
from pykbroker.exceptions import (
    BrokerRejectedError,
    ConfigurationError,
    InvalidResponseError,
    NotSupportedError,
    SpecUnverifiedError,
)
from pykbroker.experimental._rest import RestBroker
from pykbroker.models import Balance, CancelRequest, Capabilities, Order, OrderRequest, Position, Quote

LS_SPEC_VERIFIED = False  # Global veto only; True never enables live orders.
# UNVERIFIED: legacy live URL, no local official address/account-mode contract.
LS_API_BASE_URL = "https://openapi.ls-sec.co.kr:8080"
# UNVERIFIED: no distinct mock host established; do not invent one or reuse live.
LS_MOCK_BASE_URL: str | None = None


class LS(RestBroker):
    broker_code = "ls"
    supports_virtual = True  # Accept explicit mode; missing mock contract still fails closed.

    def __init__(self, *, virtual: bool = False, spec_verified: bool = False, **kwargs) -> None:
        if not isinstance(spec_verified, bool):
            raise ConfigurationError("spec_verified must be a boolean")
        super().__init__(virtual=virtual, **kwargs)
        if self._client is None:
            raise ConfigurationError("Supply an LS REST client with an explicit base_url")
        self._bound_virtual = self.virtual
        self._spec_verified = spec_verified
        self._verify_mode(self._client)

    def _verify_mode(self, client) -> None:
        expected = LS_MOCK_BASE_URL if self._bound_virtual else LS_API_BASE_URL
        if not isinstance(expected, str) or not expected or expected == LS_API_BASE_URL and self._bound_virtual:
            raise ConfigurationError("LS mock address/account-mode contract is UNVERIFIED")
        host = getattr(client, "base_url", None)
        if self.virtual is not self._bound_virtual or not isinstance(host, str) or host.rstrip("/") != expected:
            raise ConfigurationError("LS client host must match the explicit account mode")

    def _mock_verified(self) -> bool:
        return LS_SPEC_VERIFIED is True and self._spec_verified is True and self._bound_virtual is True

    def capabilities(self) -> Capabilities:
        verified = self._mock_verified()
        return Capabilities(
            virtual=self._bound_virtual,
            supports_spot=verified,
            supports_limit_order=verified,
            supports_market_order=verified,
            supports_single_order_cancel=verified,
            # UNVERIFIED: t0425 is not yet proof of real-time/history coverage.
            supports_order_history=False,
            realtime_open_orders=False,
            markets=("KR",),
            venues=("KRX",),
        )

    def _validate_order(self, request: OrderRequest | CancelRequest) -> None:
        super()._validate_order(request)
        if isinstance(request, OrderRequest) and request.order_type == "close":
            raise NotSupportedError("LS close orders have not been verified")
        if isinstance(request, CancelRequest):
            self._order_id(request.order_id, request=True)

    def place_order(self, request: OrderRequest) -> Order:
        if not self._mock_verified():
            raise SpecUnverifiedError("LS writes require a verified mock instance; live writes are locked")
        self._verify_mode(self._client)
        return super().place_order(request)

    def cancel_order(self, request: CancelRequest) -> Order:
        if not self._mock_verified():
            raise SpecUnverifiedError("LS writes require a verified mock instance; live writes are locked")
        self._verify_mode(self._client)
        return super().cancel_order(request)

    def _code(self, name: str) -> str:
        value = super()._code(name)
        if not value.strip():
            raise ConfigurationError("Unverified vendor code; supply a reviewed codes mapping")
        return value

    def _response_symbol(self, value):
        try:
            return self._symbol(value, "KR")
        except ConfigurationError:
            raise InvalidResponseError("Invalid LS response symbol") from None

    @staticmethod
    def _price(value):
        if isinstance(value, bool):
            raise InvalidResponseError("Invalid LS price")
        return optional_price(value)

    def _response(self, path, tr, body, *, write=False):
        self._verify_mode(self._client)
        result = self._request(path, tr, body)
        if not isinstance(result, dict):
            raise InvalidResponseError("Invalid LS response; reconcile writes before retrying")
        code = result.get("rsp_cd")
        if not isinstance(code, str) or not code.strip():
            raise InvalidResponseError("Missing LS response code; reconcile writes before retrying")
        # UNVERIFIED: legacy success mapping; an absent code cannot succeed.
        if code not in ("00000", "0000", "0"):
            if write:
                # Error meanings are unverified: never release a write for retry.
                raise InvalidResponseError("Unverified LS write result; reconcile before retrying")
            raise BrokerRejectedError("LS rejected the read request")
        return result

    @staticmethod
    def _order_id(value, *, request=False):
        error = ConfigurationError if request else InvalidResponseError
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise error("Invalid LS order number; reconcile writes before retrying")
        text = str(value)
        if not text.isascii() or not text.isdecimal() or not text.strip("0"):
            raise error("Invalid LS order number; reconcile writes before retrying")
        return text

    @staticmethod
    def _rows(response, block):
        rows = response[block]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise InvalidResponseError("Invalid LS rows")
        return rows

    @staticmethod
    def _complete_page(response, block, cursor):
        # UNVERIFIED: continuation; never return a partial account snapshot.
        meta = response[block]
        if not isinstance(meta, dict) or cursor not in meta or not isinstance(meta[cursor], str):
            raise InvalidResponseError("Missing LS continuation evidence")
        if meta[cursor].strip():
            raise SpecUnverifiedError("LS pagination has not been verified")

    def get_quote(self, symbol: str, *, market: str = "KR") -> Quote:
        symbol = self._symbol(symbol, market)
        return self._call(
            lambda: Quote(
                symbol=symbol,
                last=decimal(
                    self._response(
                        "/stock/market-data",
                        "t1102",
                        {"t1102InBlock": {"shcode": symbol, "exchgubun": self._code("exchange")}},
                    )["t1102OutBlock"]["price"]
                ),
            )
        )

    def get_balance(self) -> Balance:
        def read():
            row = self._response(
                "/stock/accno", "CSPAQ12200", {"CSPAQ12200InBlock1": {"BalCreTp": self._code("balance_type")}}
            )["CSPAQ12200OutBlock2"]
            return Balance(cash_available=decimal(row["MnyOrdAbleAmt"]), position_value=decimal(row["BalEvalAmt"]))

        return self._call(read)

    def get_positions(self) -> list[Position]:
        def read():
            body = {key: self._code(key) for key in ("prcgb", "chegb", "dangb", "charge")}
            body["cts_expcode"] = ""
            response = self._response("/stock/accno", "t0424", {"t0424InBlock": body})
            self._complete_page(response, "t0424OutBlock", "cts_expcode")
            return [
                Position(
                    symbol=self._response_symbol(row["expcode"]),
                    name=row["hname"],
                    quantity=whole(row["janqty"]),
                    sellable=whole(row["mdposqt"]),
                    average_price=decimal(row["pamt"]),
                    last=decimal(row["price"]),
                    market_value=decimal(row["appamt"]),
                )
                for row in self._rows(response, "t0424OutBlock1")
            ]

        return self._call(read)

    def list_orders(self, symbol: str | None = None) -> list[Order]:
        code = self._symbol(symbol, "KR") if symbol is not None else None

        def read():
            body = {
                "expcode": code if code is not None else "",
                "chegb": self._code("order_filter"),
                "medosu": self._code("side_all"),
                "sortgb": self._code("sort_order"),
                "cts_ordno": "",
            }
            response = self._response("/stock/accno", "t0425", {"t0425InBlock": body})
            self._complete_page(response, "t0425OutBlock", "cts_ordno")
            # Validate before filtering: a broken page is never partial success.
            orders = [self._order(row) for row in self._rows(response, "t0425OutBlock1")]
            return [order for order in orders if code is None or order.symbol == code]

        return self._call(read)

    def status(self, order: Order) -> Order:
        return super().status(order).model_copy(update={"virtual": self._bound_virtual})

    def _order(self, row) -> Order:
        # UNVERIFIED (명세 미확인): candidate t0425 fields. Missing fill data stays None.
        quantity = whole(row["qty"]) if self._mock_verified() and "qty" in row else None
        filled = whole(row["cheqty"]) if self._mock_verified() and "cheqty" in row else None
        remaining = whole(row["ordrem"])
        price = self._price(row.get("price"))
        if quantity is not None:
            if quantity <= 0 or remaining > quantity or (filled is not None and filled + remaining > quantity):
                raise InvalidResponseError("Inconsistent LS order quantities")
        status = "unknown"
        # No quantity subtraction, zero-remainder inference, or unverified state interpretation.
        if self._mock_verified() and quantity is not None and filled is not None:
            if filled == quantity and remaining == 0:
                status = "filled"
            elif filled > 0 and remaining > 0:
                status = "partial"
            elif filled == 0 and remaining == quantity:
                status = "open"
        return Order(
            id=self._order_id(row["ordno"]),
            symbol=self._response_symbol(row["expcode"]),
            quantity=quantity,
            price=price,
            filled=filled,
            # UNVERIFIED: cheprice is not established as the average execution price.
            fill_price=None,
            remaining=remaining,
            status=status,
            virtual=self._bound_virtual,
        )

    def _place_order(self, request: OrderRequest) -> Order:
        body = {
            "IsuNo": self._symbol(request.symbol, "KR"),
            "OrdQty": request.quantity,
            "OrdPrc": int(request.price or 0),
            "BnsTpCode": self._code(request.side),
            "OrdprcPtnCode": self._code(request.order_type),
            "MgntrnCode": self._code("cash"),
            "LoanDt": "",
            "OrdCndiTpCode": self._code("condition"),
            "MbrNo": self._code("member"),
        }
        response = self._response("/stock/order", "CSPAT00601", {"CSPAT00601InBlock1": body}, write=True)
        return Order(
            id=self._order_id(response["CSPAT00601OutBlock2"]["OrdNo"]),
            symbol=self._symbol(request.symbol, "KR"),
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            price=request.price,
            status="accepted",
            virtual=self._bound_virtual,
        )

    def _cancel_order(self, request: CancelRequest) -> Order:
        body = {"OrgOrdNo": request.order_id, "IsuNo": self._symbol(request.symbol, "KR"), "OrdQty": request.quantity}
        response = self._response("/stock/order", "CSPAT00801", {"CSPAT00801InBlock1": body}, write=True)
        # UNVERIFIED: ack block; accepted does not prove the original order closed.
        self._order_id(response["CSPAT00801OutBlock2"]["OrdNo"])
        return Order(
            id=request.order_id,
            symbol=self._symbol(request.symbol, "KR"),
            status="accepted",
            virtual=self._bound_virtual,
        )
