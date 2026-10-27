"""Unverified Kiwoom domestic mapping with caller-owned REST/authentication."""

from pykbroker.brokers._common import decimal, whole
from pykbroker.exceptions import BrokerRejectedError, SpecUnverifiedError
from pykbroker.models import Balance, CancelRequest, Order, OrderRequest, Position, Quote

from ._rest import RestBroker

KIWOOM_SPEC_VERIFIED = False


class Kiwoom(RestBroker):
    broker_code = "kiwoom"

    def place_order(self, request: OrderRequest) -> Order:
        if KIWOOM_SPEC_VERIFIED is not True:
            raise SpecUnverifiedError("KIWOOM_SPEC_VERIFIED is not True")
        return super().place_order(request)

    def cancel_order(self, request: CancelRequest) -> Order:
        if KIWOOM_SPEC_VERIFIED is not True:
            raise SpecUnverifiedError("KIWOOM_SPEC_VERIFIED is not True")
        return super().cancel_order(request)

    def _response(self, path, tr, body):
        response = self._request(path, tr, body)
        if response.get("return_code") not in (None, 0, "0"):
            raise BrokerRejectedError("Kiwoom rejected the request")
        return response

    def get_quote(self, symbol: str, *, market: str = "KR") -> Quote:
        symbol = self._symbol(symbol, market)
        return self._call(
            lambda: Quote(
                symbol=symbol,
                last=decimal(self._response("/api/dostk/stkinfo", "ka10001", {"stk_cd": symbol})["cur_prc"]),
            )
        )

    def _balance_response(self):
        return self._response(
            "/api/dostk/acnt", "kt00018", {"dmst_stex_tp": self._code("exchange"), "qry_tp": self._code("query_type")}
        )

    def get_balance(self) -> Balance:
        raise SpecUnverifiedError("Experimental Kiwoom balance is disabled; use pykbroker.brokers.kiwoom.Kiwoom")

    def get_positions(self) -> list[Position]:
        return self._call(
            lambda: [
                Position(
                    symbol=self._symbol(row["stk_cd"], "KR"),
                    name=row["stk_nm"],
                    quantity=whole(row["rmnd_qty"]),
                    sellable=whole(row["trde_able_qty"]),
                    average_price=decimal(row["pur_pric"]),
                    last=decimal(row["cur_prc"]),
                    market_value=decimal(row["evlt_amt"]),
                )
                for row in self._balance_response()["acnt_evlt_remn_indv_tot"]
            ]
        )

    def _place_order(self, request: OrderRequest) -> Order:
        body = {
            "stk_cd": self._symbol(request.symbol, "KR"),
            "ord_qty": str(request.quantity),
            "ord_uv": format(request.price, "f") if request.price is not None else "0",
            "trde_tp": self._code(request.order_type),
            "dmst_stex_tp": self._code("exchange"),
            "cond_uv": "",
        }
        response = self._response("/api/dostk/ordr", "kt10000" if request.side == "buy" else "kt10001", body)
        return Order(id=str(response["ord_no"]), symbol=request.symbol, status="accepted")

    # TODO: verified open-order and single-cancel schemas; inherited methods refuse both.
