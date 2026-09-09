"""Unverified LS domestic request mapping; the specification lock stays closed."""

from pykbroker.brokers._common import decimal, whole
from pykbroker.exceptions import BrokerRejectedError, SpecUnverifiedError
from pykbroker.models import Balance, CancelRequest, Order, OrderRequest, Position, Quote

from ._rest import RestBroker

LS_SPEC_VERIFIED = False


class LS(RestBroker):
    broker_code = "ls"

    def place_order(self, request: OrderRequest) -> Order:
        if LS_SPEC_VERIFIED is not True:
            raise SpecUnverifiedError("LS_SPEC_VERIFIED is not True")
        return super().place_order(request)

    def cancel_order(self, request: CancelRequest) -> Order:
        if LS_SPEC_VERIFIED is not True:
            raise SpecUnverifiedError("LS_SPEC_VERIFIED is not True")
        return super().cancel_order(request)

    def _response(self, path, tr, body):
        result = self._request(path, tr, body)
        if result.get("rsp_cd") not in (None, "00000", "0000", "0"):
            raise BrokerRejectedError("LS rejected the request")
        return result

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
            if response.get("t0424OutBlock", {}).get("cts_expcode"):
                raise SpecUnverifiedError("LS pagination has not been verified")
            return [
                Position(
                    symbol=row["expcode"],
                    name=row["hname"],
                    quantity=whole(row["janqty"]),
                    sellable=whole(row["mdposqt"]),
                    average_price=decimal(row["pamt"]),
                    last=decimal(row["price"]),
                    market_value=decimal(row["appamt"]),
                )
                for row in response["t0424OutBlock1"]
            ]

        return self._call(read)

    def list_orders(self, symbol: str | None = None) -> list[Order]:
        def read():
            body = {
                "expcode": self._symbol(symbol, "KR") if symbol else "",
                "chegb": self._code("order_filter"),
                "medosu": self._code("side_all"),
                "sortgb": self._code("sort_order"),
                "cts_ordno": "",
            }
            response = self._response("/stock/accno", "t0425", {"t0425InBlock": body})
            if response.get("t0425OutBlock", {}).get("cts_ordno"):
                raise SpecUnverifiedError("LS pagination has not been verified")
            return [
                Order(id=str(row["ordno"]), symbol=row["expcode"], remaining=whole(row["ordrem"]), status="unknown")
                for row in response["t0425OutBlock1"]
                if symbol is None or row["expcode"] == symbol
            ]

        return self._call(read)

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
        response = self._response("/stock/order", "CSPAT00601", {"CSPAT00601InBlock1": body})
        return Order(id=str(response["CSPAT00601OutBlock2"]["OrdNo"]), symbol=request.symbol, status="accepted")

    def _cancel_order(self, request: CancelRequest) -> Order:
        body = {"OrgOrdNo": request.order_id, "IsuNo": self._symbol(request.symbol, "KR"), "OrdQty": request.quantity}
        self._response("/stock/order", "CSPAT00801", {"CSPAT00801InBlock1": body})
        return Order(id=request.order_id, symbol=request.symbol, status="accepted")
